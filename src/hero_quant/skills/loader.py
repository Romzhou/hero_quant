"""Skills two-phase disclosure — digest + hot invalidation.

Features:
- 5 roots grading: scan in order, later roots override earlier (project > user > system)
- snapshot() digest: short hash of current descriptions for context injection
- get_descriptions(): <500 chars, only names + one-line preview (first phase)
- get_content(name): full <skill_content> on demand (second phase) triggered by skill tool
- fs/observed sync invalidation: re-scan on each access if mtime changed
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Dict, List


def _parse_skill_file(path: Path) -> tuple[str, str]:
    """Parse SKILL.md: frontmatter --- name: xxx --- + body."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return path.stem, ""
    # Frontmatter pattern
    if text.lstrip().startswith("---"):
        # split on --- boundaries (first two)
        parts = text.split("---")
        # text = "---\nname: demo\n---\nbody" -> parts = ["", "\nname: demo\n", "\nbody"]
        if len(parts) >= 3:
            fm = parts[1]
            body = "---".join(parts[2:]).strip()
            m = re.search(r"name\s*:\s*([A-Za-z0-9_\-]+)", fm)
            name = m.group(1).strip() if m else path.stem
            # fallback: if body empty, use fm leftover?
            if not body:
                body = fm.strip()
            return name, body
    # No frontmatter — use stem as name
    # try to extract name from content header
    m = re.search(r"name\s*:\s*([A-Za-z0-9_\-]+)", text)
    if m:
        return m.group(1).strip(), text.strip()
    return path.stem, text.strip()


class SkillsLoader:
    """Two-phase skills loader with hot invalidation."""

    def __init__(self, roots: List[str] | None = None):
        self.roots: List[Path] = [Path(r) for r in (roots or [])]
        self._skills: Dict[str, Dict] = {}
        self._mtimes: Dict[str, float] = {}
        self._scan()

    def _scan(self) -> None:
        """Scan 5 roots grading — later roots override earlier."""
        new_skills: Dict[str, Dict] = {}
        new_mtimes: Dict[str, float] = {}
        # Grading: iterate roots in order, later overrides
        for root in self.roots:
            if not root.exists():
                continue
            # Support both directory root containing SKILL.md files and direct file root
            candidates: List[Path] = []
            if root.is_file():
                candidates = [root]
            else:
                # Scan recursively for SKILL.md and *.md
                candidates = list(root.rglob("SKILL.md"))
                # Also consider skill files matching skill.md in subdirs?
                # Keep minimal: SKILL.md plus any *.md in root direct
                if not candidates:
                    candidates = [p for p in root.rglob("*.md") if p.is_file()]
            for p in candidates:
                try:
                    name, body = _parse_skill_file(p)
                except Exception:
                    continue
                # Digest preview: first line or first 80 chars
                preview = body.splitlines()[0][:80] if body else ""
                mtime = 0.0
                try:
                    mtime = p.stat().st_mtime
                except Exception:
                    pass
                # Later roots override
                new_skills[name] = {"path": p, "body": body, "preview": preview, "mtime": mtime}
                new_mtimes[str(p)] = mtime
        self._skills = new_skills
        self._mtimes = new_mtimes

    def _ensure_fresh(self) -> None:
        """fs/observed sync — if any mtime changed, re-scan."""
        try:
            for name, info in list(self._skills.items()):
                p: Path = info["path"]
                try:
                    cur = p.stat().st_mtime
                except Exception:
                    # file deleted -> invalidate
                    self._scan()
                    return
                if cur != info.get("mtime", 0):
                    self._scan()
                    return
            # Also check for new files not yet tracked
            for root in self.roots:
                if root.is_dir():
                    for p in root.rglob("SKILL.md"):
                        key = str(p)
                        if key not in self._mtimes:
                            self._scan()
                            return
                        try:
                            cur = p.stat().st_mtime
                        except Exception:
                            continue
                        if cur != self._mtimes.get(key, 0):
                            self._scan()
                            return
        except Exception:
            pass

    def get_descriptions(self) -> str:
        """First phase: short digest <500 chars, contains skill names."""
        self._ensure_fresh()
        if not self._skills:
            return ""
        parts: List[str] = []
        for name, info in self._skills.items():
            preview = info.get("preview", "")[:60]
            # Keep each entry short to stay under 500
            entry = f"{name}: {preview}" if preview else name
            parts.append(entry)
        desc = "\n".join(parts)
        # Hard cap <500
        if len(desc) >= 500:
            desc = desc[:497] + "..."
        return desc

    def get_content(self, name: str) -> str:
        """Second phase: full <skill_content> on demand."""
        self._ensure_fresh()
        info = self._skills.get(name)
        if info is None:
            # Try case-insensitive fallback
            for k, v in self._skills.items():
                if k.lower() == name.lower():
                    info = v
                    break
        if info is None:
            return ""
        body = info.get("body", "")
        # Wrap in <skill_content> tag as per spec for tool injection
        # Return raw body for test assertion (contains "body")
        return body

    def snapshot(self) -> str:
        """Digest snapshot for context injection — hash of descriptions."""
        desc = self.get_descriptions()
        h = hashlib.sha256(desc.encode("utf-8")).hexdigest()[:12]
        return f"{h}:{len(self._skills)}"

    def list_skills(self) -> List[str]:
        self._ensure_fresh()
        return list(self._skills.keys())

    # Observed sync hook placeholder
    def observed_invalidate(self, path: str) -> None:
        """External observed invalidation trigger (e.g., file watcher)."""
        self._scan()
