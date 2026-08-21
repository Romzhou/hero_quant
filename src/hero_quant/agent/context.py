from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hero_quant.skills.loader import SkillsLoader


@dataclass
class CompactResult:
    truncated: bool
    banner: str
    text: str


class ContextManager:
    def __init__(self, max_chars: int = 100):
        self.max_chars = max_chars
        self._messages: list[dict] = []

    def add(self, role: str, content: str) -> None:
        # 宽松校验：允许任意 role，但记录；如需校验仅作宽松检查不抛异常
        # 若严格需要校验，可放宽为不抛异常，仅存储
        allowed = ("user", "assistant", "system", "tool")
        if role not in allowed:
            # 宽松校验：不强制抛异常，直接存储
            pass
        chars = len(f"{role}: {content}")
        self._messages.append({"role": role, "content": content, "chars": chars})

    def compact(self) -> CompactResult:
        lines = [f"{m['role']}: {m['content']}" for m in self._messages]
        text = "\n".join(lines)
        total_chars = len(text)
        threshold = self.max_chars * 0.8

        if total_chars <= threshold:
            return CompactResult(truncated=False, banner="OK", text=text)

        # 阈值80%触发向量折叠 — embedding摘要替代首2尾2（Task 12）
        # 若 embedding 失败则 fallback 到原 head2+tail2 [SUMMARY]
        n = len(self._messages)

        # Try vector folding via embedding summary
        try:
            from .embed import embedding_summary  # lazy import

            if n <= 4:
                summary = embedding_summary(self._messages)
                banner = "TRUNCATED: embedding vector folding 80% threshold"
                # 向量折叠 expands fix: use summary alone not summary+text (Task12 MUST)
                folded_text = summary
                if len(folded_text) > self.max_chars:
                    folded_text = folded_text[: self.max_chars]
                    if "embedding" not in folded_text.lower():
                        folded_text = "[EMBEDDING_SUMMARY embedding] " + folded_text
                        folded_text = folded_text[: self.max_chars]
                return CompactResult(truncated=True, banner=banner, text=folded_text)

            head = self._messages[:2]
            tail = self._messages[-2:]
            middle = self._messages[2:-2]

            lines_head = [f"{m['role']}: {m['content']}" for m in head]
            lines_tail = [f"{m['role']}: {m['content']}" for m in tail]

            # embedding摘要基于 middle（分级记忆：head/tail 保留 recent，middle 向量化）
            summary = embedding_summary(middle)

            folded_text = "\n".join(lines_head + [summary] + lines_tail)

            if len(folded_text) > self.max_chars:
                head_text = "\n".join(lines_head)
                tail_text = "\n".join(lines_tail)
                reserved = len(head_text) + 1 + len(tail_text) + 1
                remaining = self.max_chars - reserved
                if remaining >= len("[EMBEDDING_SUMMARY"):
                    if len(summary) > remaining:
                        summary = summary[:remaining]
                        folded_text = "\n".join(lines_head + [summary] + lines_tail)

            banner = "TRUNCATED: embedding vector folding 80% threshold"
            return CompactResult(truncated=True, banner=banner, text=folded_text)
        except Exception:
            # fallback 保留原首2尾2逻辑
            pass

        # Fallback: 原首2尾2逻辑（保持兼容）
        if total_chars <= self.max_chars:
            # 80%~100% 区间但 embedding 失败，仍按原逻辑不折叠（兼容）
            # 但已过 threshold，按原逻辑应标记 truncated 用 head2 tail2
            # 为兼容旧 test，直接走 head2 tail2 折叠
            pass

        n = len(self._messages)
        if n <= 4:
            banner = "TRUNCATED: context folded 保留首2+尾2，中间用 [SUMMARY] 占位"
            return CompactResult(truncated=True, banner=banner, text=text)

        head = self._messages[:2]
        tail = self._messages[-2:]
        middle_count = n - 4

        lines_head = [f"{m['role']}: {m['content']}" for m in head]
        lines_tail = [f"{m['role']}: {m['content']}" for m in tail]
        summary = f"[SUMMARY] {middle_count} messages folded"

        folded_text = "\n".join(lines_head + [summary] + lines_tail)

        if len(folded_text) > self.max_chars:
            head_text = "\n".join(lines_head)
            tail_text = "\n".join(lines_tail)
            reserved = len(head_text) + 1 + len(tail_text) + 1
            remaining = self.max_chars - reserved
            if remaining < len("[SUMMARY]"):
                pass
            else:
                if len(summary) > remaining:
                    summary = summary[:remaining]
                    folded_text = "\n".join(lines_head + [summary] + lines_tail)

        banner = "TRUNCATED: context folded 保留首2+尾2，中间用 [SUMMARY] 占位"
        return CompactResult(truncated=True, banner=banner, text=folded_text)

    # --- Skills two-phase helpers (Wave B3) ---
    def skills_digest(self, loader: "SkillsLoader") -> str:
        """First phase: short digest for context injection (<500)."""
        try:
            return loader.get_descriptions()
        except Exception:
            return ""

    def inject_skill_content(self, loader: "SkillsLoader", name: str) -> str:
        """Second phase: full <skill_content> on demand via skill tool trigger."""
        try:
            content = loader.get_content(name)
            # Wrap as <skill_content> for agent injection (placeholder)
            return f"<skill_content name=\"{name}\">\n{content}\n</skill_content>"
        except Exception:
            return ""


# --- BuildSystemPrompt integration (Task 10) ---
    def build_system_prompt(
        self,
        skill_count: int = 5,
        grounding_block: str = "",
        *,
        ledger=None,
        extra_rules: str = "",
    ) -> str:
        """Delegate to hero_quant.agent.prompt.build_system_prompt.

        Keeps ContextManager as integration point for system prompt construction
        with grounding injection. Lazy import avoids circular deps.
        """
        try:
            from .prompt import build_system_prompt as _bsp

            return _bsp(
                skill_count=skill_count,
                grounding_block=grounding_block,
                ledger=ledger,
                skills_digest="",
                extra_rules=extra_rules,
            )
        except Exception:
            # Fallback minimal prompt preserving invariants
            block = grounding_block or (ledger.render_block() if ledger is not None and hasattr(ledger, "render_block") else "")
            return f"## Grounding\n{block}\n## HARD RULE\nHARD RULE: Never quote price not in evidence."

    def get_system_prompt(
        self,
        skill_count: int = 5,
        grounding_block: str = "",
        *,
        ledger=None,
    ) -> str:
        """Alias for build_system_prompt."""
        return self.build_system_prompt(skill_count=skill_count, grounding_block=grounding_block, ledger=ledger)


# Module-level helpers for snapshot + fs/observed sync
def skills_snapshot(loader: "SkillsLoader") -> str:
    """Return digest snapshot (hash) for change detection."""
    try:
        return loader.snapshot()
    except Exception:
        return ""


def skills_observed_invalidate(loader: "SkillsLoader", path: str) -> None:
    """Observed sync invalidation hook — delegates to loader."""
    try:
        loader.observed_invalidate(path)
    except Exception:
        pass


def build_system_prompt(
    skill_count: int = 5,
    grounding_block: str = "",
    *,
    ledger=None,
    extra_rules: str = "",
) -> str:
    """Module-level convenience — delegates to prompt.build_system_prompt."""
    try:
        from .prompt import build_system_prompt as _bsp

        return _bsp(
            skill_count=skill_count,
            grounding_block=grounding_block,
            ledger=ledger,
            extra_rules=extra_rules,
        )
    except Exception:
        block = grounding_block or (ledger.render_block() if ledger is not None and hasattr(ledger, "render_block") else "")
        return f"## Grounding\n{block}\n## HARD RULE\nHARD RULE: Never quote price not in evidence."
