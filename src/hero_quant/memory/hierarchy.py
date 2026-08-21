"""Hierarchical directory routing for memory entries.

Minimal port of vibe memory/hierarchy.py 70-379.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

CATEGORIES = ("user", "feedback", "project", "reference")

_SKIP_NAMES = frozenset({"MEMORY.md", ".hierarchy.yaml", ".lock", "archive", "gc.log"})


@dataclass
class CategorySummary:
    count: int = 0
    keywords: List[str] = field(default_factory=list)


class MemoryHierarchy:
    """Manages hierarchical directory structure for memory entries.

    Provides routed access to memory files organized by memory_type,
    while maintaining backward compatibility with flat-stored entries.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._base_dir / ".hierarchy.yaml"

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def _ensure_category_dir(self, category: str) -> Path:
        cat_dir = self._base_dir / category
        cat_dir.mkdir(parents=True, exist_ok=True)
        return cat_dir

    def route_entry(self, memory_type: str, filename: str) -> Path:
        """Determine storage path: base_dir/{memory_type}/{filename}.

        Creates category directory on demand. Falls back to base_dir for unknown types.
        """
        if memory_type in CATEGORIES:
            cat_dir = self._ensure_category_dir(memory_type)
            return cat_dir / filename
        if memory_type:
            logger.warning("Unknown memory_type '%s', routing to base dir", memory_type)
        return self._base_dir / filename

    def recover_extensionless_entries(self) -> List[Path]:
        recovered: List[Path] = []
        for category in CATEGORIES:
            cat_dir = self._base_dir / category
            if not cat_dir.is_dir():
                continue
            for item in sorted(cat_dir.iterdir()):
                if item.name in _SKIP_NAMES or not item.is_file():
                    continue
                if item.suffix:
                    continue
                target = item.with_suffix(".md")
                if target.exists():
                    continue
                try:
                    head = item.read_text(encoding="utf-8")[:3]
                except OSError:
                    continue
                if head.strip() != "---":
                    continue
                try:
                    item.rename(target)
                    recovered.append(target)
                except OSError:
                    continue
        return recovered

    def scan_all(self) -> List[Path]:
        """Scan base dir + all category subdirs for *.md files. Skips archive."""
        self.recover_extensionless_entries()
        results: List[Path] = []
        if self._base_dir.is_dir():
            for item in self._base_dir.iterdir():
                if item.name in _SKIP_NAMES:
                    continue
                if item.is_file() and item.suffix == ".md":
                    results.append(item)
        for category in CATEGORIES:
            cat_dir = self._base_dir / category
            if cat_dir.is_dir():
                for item in cat_dir.iterdir():
                    if item.is_file() and item.suffix == ".md":
                        results.append(item)
        results.sort(key=lambda p: p.name)
        return results

    def scan_category(self, category: str) -> List[Path]:
        cat_dir = self._base_dir / category
        results: List[Path] = []
        if not cat_dir.is_dir():
            return results
        for item in cat_dir.iterdir():
            if item.is_file() and item.suffix == ".md":
                results.append(item)
        results.sort(key=lambda p: p.name)
        return results

    def rebuild_index(self, entries: list) -> None:
        cat_data: Dict[str, CategorySummary] = {cat: CategorySummary() for cat in CATEGORIES}
        for entry in entries:
            mtype = entry.get("memory_type", "") if isinstance(entry, dict) else getattr(entry, "memory_type", "")
            if mtype not in cat_data:
                continue
            cat_data[mtype].count += 1
            keywords = entry.get("keywords", []) if isinstance(entry, dict) else []
            if isinstance(keywords, list):
                cat_data[mtype].keywords.extend(keywords)
        max_keywords = 10
        for summary in cat_data.values():
            seen: Set[str] = set()
            unique: List[str] = []
            for kw in summary.keywords:
                kw_lower = kw.lower().strip()
                if kw_lower and kw_lower not in seen:
                    seen.add(kw_lower)
                    unique.append(kw_lower)
            summary.keywords = unique[:max_keywords]
        rebuilt_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        lines: List[str] = [
            "# Auto-generated memory hierarchy index",
            f'rebuilt_at: "{rebuilt_at}"',
            "categories:",
        ]
        for cat in CATEGORIES:
            summary = cat_data[cat]
            kw_list = ", ".join(summary.keywords)
            lines.append(f"  {cat}:")
            lines.append(f"    count: {summary.count}")
            lines.append(f"    keywords: [{kw_list}]")
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _parse_index_keywords(self) -> Dict[str, List[str]]:
        if not self._index_path.is_file():
            return {}
        result: Dict[str, List[str]] = {}
        current_cat: Optional[str] = None
        try:
            text = self._index_path.read_text(encoding="utf-8")
        except OSError:
            return {}
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.endswith(":") and not stripped.startswith("#") and not stripped.startswith("categories") and not stripped.startswith("rebuilt_at"):
                cat_name = stripped.rstrip(":")
                if cat_name in CATEGORIES:
                    current_cat = cat_name
                    if current_cat not in result:
                        result[current_cat] = []
                else:
                    current_cat = None
            elif stripped.startswith("keywords:") and current_cat:
                bracket_start = stripped.find("[")
                bracket_end = stripped.find("]")
                if bracket_start != -1 and bracket_end != -1:
                    inner = stripped[bracket_start + 1 : bracket_end]
                    keywords = [k.strip() for k in inner.split(",") if k.strip()]
                    result[current_cat] = keywords
        return result

    def prune_search_scope(self, query_tokens: Set[str], category_filter: str = "") -> List[Path]:
        if category_filter:
            if category_filter in CATEGORIES:
                return self.scan_category(category_filter)
            return self.scan_all()
        index_keywords = self._parse_index_keywords()
        if not index_keywords:
            return self.scan_all()
        scored: List[tuple] = []
        for cat in CATEGORIES:
            cat_kws = set(index_keywords.get(cat, []))
            overlap = len(query_tokens & cat_kws)
            scored.append((overlap, cat))
        scored.sort(key=lambda x: x[0], reverse=True)
        results: List[Path] = []
        seen: Set[Path] = set()
        for _score, cat in scored:
            for p in self.scan_category(cat):
                if p not in seen:
                    results.append(p)
                    seen.add(p)
        if self._base_dir.is_dir():
            for item in self._base_dir.iterdir():
                if item.name in _SKIP_NAMES:
                    continue
                if item.is_file() and item.suffix == ".md" and item not in seen:
                    results.append(item)
                    seen.add(item)
        return results

    def migrate_flat_entry(self, file_path: Path, memory_type: str) -> Optional[Path]:
        if not file_path.is_file():
            return None
        if file_path.parent != self._base_dir:
            return None
        if memory_type not in CATEGORIES:
            return None
        dest_dir = self._ensure_category_dir(memory_type)
        dest_path = dest_dir / file_path.name
        if dest_path.exists():
            return None
        try:
            file_path.rename(dest_path)
            return dest_path
        except OSError:
            return None
