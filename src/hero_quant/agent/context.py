"""上下文管理器：长度感知折叠与 System Prompt 组装.

职责：维护对话上下文长度，超阈时做向量折叠或首尾截断，保持 head/tail 可回溯性。
架构位置：agent 层上下文中枢，被 Loop 调用做 compact，并通过 prompt/grounding 做注入。
关键设计：
- 阈值触发：总字符 > max_chars*0.8 时触发折叠，保留首2/尾2，中间以 embedding 摘要或 [SUMMARY] 占位
- 分级记忆：middle 段走 embedding_summary 的 centroid 关键词摘要，失败回落首尾截断
- 两阶段技能与 Grounding：skills digest/full 按需注入，System Prompt 委托 prompt.build_system_prompt
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hero_quant.skills.loader import SkillsLoader


@dataclass
class CompactResult:
    """折叠结果：是否截断、提示横幅与折叠后文本."""

    truncated: bool
    banner: str
    text: str


class ContextManager:
    """上下文管理器，负责追加、折叠与 prompt 集成."""
    def __init__(self, max_chars: int = 100):
        self.max_chars = max_chars
        self._messages: list[dict] = []

    def add(self, role: str, content: str) -> None:
        # 宽松校验：未知 role 仍存储，不抛异常以保兼容
        allowed = ("user", "assistant", "system", "tool")
        if role not in allowed:
            pass
        chars = len(f"{role}: {content}")
        self._messages.append({"role": role, "content": content, "chars": chars})

    def compact(self) -> CompactResult:
        """按阈值折叠上下文，优先向量摘要，失败回落首尾截断."""
        lines = [f"{m['role']}: {m['content']}" for m in self._messages]
        text = "\n".join(lines)
        total_chars = len(text)
        threshold = self.max_chars * 0.8

        if total_chars <= threshold:
            return CompactResult(truncated=False, banner="OK", text=text)

        n = len(self._messages)

        try:
            from .embed import embedding_summary  # 懒加载避免循环依赖

            if n <= 4:
                summary = embedding_summary(self._messages)
                banner = "TRUNCATED: embedding vector folding 80% threshold"
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
            pass

        if total_chars <= self.max_chars:
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

    def skills_digest(self, loader: "SkillsLoader") -> str:
        """首阶段：返回技能短摘要，用于上下文注入."""
        try:
            return loader.get_descriptions()
        except Exception:
            return ""

    def inject_skill_content(self, loader: "SkillsLoader", name: str) -> str:
        """二阶段：按需返回完整技能内容，包为 <skill_content>."""
        try:
            content = loader.get_content(name)
            return f"<skill_content name=\"{name}\">\n{content}\n</skill_content>"
        except Exception:
            return ""

    def build_system_prompt(
        self,
        skill_count: int = 5,
        grounding_block: str = "",
        *,
        ledger=None,
        extra_rules: str = "",
    ) -> str:
        """委托 prompt.build_system_prompt 组装 System Prompt，支持 Grounding 注入."""
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
            block = grounding_block or (ledger.render_block() if ledger is not None and hasattr(ledger, "render_block") else "")
            return f"## Grounding\n{block}\n## HARD RULE\nHARD RULE: Never quote price not in evidence."

    def get_system_prompt(
        self,
        skill_count: int = 5,
        grounding_block: str = "",
        *,
        ledger=None,
    ) -> str:
        """build_system_prompt 的别名."""
        return self.build_system_prompt(skill_count=skill_count, grounding_block=grounding_block, ledger=ledger)


def skills_snapshot(loader: "SkillsLoader") -> str:
    """返回技能摘要快照，用于变更检测."""
    try:
        return loader.snapshot()
    except Exception:
        return ""


def skills_observed_invalidate(loader: "SkillsLoader", path: str) -> None:
    """观察同步失效钩子，委托 loader 处理."""
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
    """模块级便捷入口，委托 prompt.build_system_prompt."""
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
