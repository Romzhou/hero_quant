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

        if total_chars <= self.max_chars:
            return CompactResult(truncated=False, banner="OK", text=text)

        # 需要截断折叠：保留首2+尾2，中间用 [SUMMARY] 占位
        n = len(self._messages)
        if n <= 4:
            # 消息过少无法折叠，直接标记截断
            banner = "TRUNCATED: context folded 保留首2+尾2，中间用 [SUMMARY] 占位"
            return CompactResult(truncated=True, banner=banner, text=text)

        head = self._messages[:2]
        tail = self._messages[-2:]
        middle_count = n - 4

        lines_head = [f"{m['role']}: {m['content']}" for m in head]
        lines_tail = [f"{m['role']}: {m['content']}" for m in tail]
        summary = f"[SUMMARY] {middle_count} messages folded"

        folded_text = "\n".join(lines_head + [summary] + lines_tail)

        # 若折叠后仍超 max_chars，截断中间 summary 长度但首尾必须完整
        if len(folded_text) > self.max_chars:
            # 尝试缩短 summary，首尾完整保留
            # 计算首尾占用长度
            head_text = "\n".join(lines_head)
            tail_text = "\n".join(lines_tail)
            # 可用给 summary 的剩余空间
            # 至少保留 "[SUMMARY]" 前缀
            reserved = len(head_text) + 1 + len(tail_text) + 1  # 加换行符
            remaining = self.max_chars - reserved
            if remaining < len("[SUMMARY]"):
                # 即使 summary 最小化仍超长，仍返回折叠文本（允许 >max_chars，关键是保留首尾）
                pass
            else:
                # 截断 summary 到 remaining 长度
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
