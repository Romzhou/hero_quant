"""上下文管理器：长度感知折叠与 System Prompt 组装.

职责：维护对话上下文长度，超阈时做向量折叠或首尾截断，保持 head/tail 可回溯性。
架构位置：agent 层上下文中枢，被 Loop 调用做 compact，并通过 prompt/grounding 做注入。
关键设计：
- 阈值触发：总字符 > max_chars*0.8 时触发折叠，保留首2/尾2，中间以 embedding 摘要或 [SUMMARY] 占位
- 分级记忆：middle 段走 embedding_summary 的 centroid 关键词摘要，失败回落首尾截断
- 两阶段技能与 Grounding：skills digest/full 按需注入，System Prompt 委托 prompt.build_system_prompt
"""

import functools
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from hero_quant.skills.loader import SkillsLoader


@functools.lru_cache(maxsize=1)
def _cached_auto_digest() -> str:
    """缓存 auto-discover 的 digest，避免重复扫描文件系统."""
    try:
        from hero_quant.skills.loader import SkillsLoader
        from pathlib import Path as _Path

        _roots: list[str] = []
        for cand in [_Path("skills"), _Path("src/hero_quant/skills"), _Path(__file__).resolve().parents[2] / "skills"]:
            try:
                if cand.exists():
                    _roots.append(str(cand))
            except Exception as exc:
                logger.debug("digest root check failed for %s: %s", cand, exc)
                continue
        if _roots:
            try:
                _auto_loader = SkillsLoader(roots=_roots)
                return _auto_loader.get_descriptions() or ""
            except Exception as exc:
                logger.warning("auto digest load failed for roots %r: %s", _roots, exc, exc_info=True)
                return ""
        try:
            _auto_loader = SkillsLoader(roots=["skills"])
            return _auto_loader.get_descriptions() or ""
        except Exception as exc:
            logger.debug("auto digest fallback failed: %s", exc)
            return ""
    except Exception as exc:
        logger.warning("cached auto digest unexpected failure: %s", exc, exc_info=True)
        return ""


@dataclass
class CompactResult:
    """折叠结果：是否截断、提示横幅与折叠后文本."""

    truncated: bool
    banner: str
    text: str


class ContextManager:
    """上下文管理器，负责追加、折叠与 prompt 集成."""
    def __init__(self, max_chars: int = 100):
        try:
            max_chars = int(max_chars)
        except (ValueError, TypeError) as exc:
            logger.warning("invalid max_chars %r: %s, using 100", max_chars, exc, exc_info=True)
            max_chars = 100
        if max_chars <= 0:
            logger.warning("max_chars must be >0 got %r, clamped to 100", max_chars)
            max_chars = 100
        self.max_chars = max_chars
        # Defensive copy: callers must not share mutable list
        self._messages: list[dict] = []

    def add(self, role: str, content: str) -> None:
        allowed = ("user", "assistant", "system", "tool")
        if role not in allowed:
            raise ValueError(f"invalid role: {role!r}, allowed={allowed}")
        if not isinstance(content, str):
            logger.warning("content coerced from %s to str", type(content).__name__)
            content = str(content)
        chars = len(f"{role}: {content}")
        # Store defensive copy to avoid mutable shared state across tenants
        self._messages.append({"role": role, "content": content, "chars": chars})

    @staticmethod
    def _render_messages(messages: list[dict]) -> str:
        return "\n".join(f"{message['role']}: {message['content']}" for message in messages)

    @staticmethod
    def _microcompact(messages: list[dict]) -> tuple[list[dict], bool]:
        """折叠旧工具结果，但保留最近三条工具结果供后续推理使用."""
        tool_positions = [index for index, message in enumerate(messages) if message.get("role") == "tool"]
        folded_positions = set(tool_positions[:-3])
        if not folded_positions:
            return messages, False

        folded = []
        marker_added = False
        marker = f"[MICROCOMPACT] {len(folded_positions)} older tool results folded"
        for index, message in enumerate(messages):
            if index in folded_positions:
                if not marker_added:
                    folded.append({"role": "system", "content": marker, "chars": len(f"system: {marker}")})
                    marker_added = True
                continue
            folded.append(message)
        return folded, True

    def _embedding_compact(self, messages: list[dict]) -> CompactResult:
        """保留原有 embedding summary 的首尾保护行为."""
        from .embed import embedding_summary  # 懒加载避免循环依赖

        n = len(messages)
        if n <= 4:
            summary = embedding_summary(messages)
            folded_text = summary
            if len(folded_text) > self.max_chars:
                folded_text = folded_text[: self.max_chars]
                if "embedding" not in folded_text.lower():
                    folded_text = "[EMBEDDING_SUMMARY embedding] " + folded_text
                    folded_text = folded_text[: self.max_chars]
            return CompactResult(
                truncated=True,
                banner="TRUNCATED: embedding vector folding 80% threshold",
                text=folded_text,
            )

        head = messages[:2]
        tail = messages[-2:]
        middle = messages[2:-2]
        lines_head = [f"{message['role']}: {message['content']}" for message in head]
        lines_tail = [f"{message['role']}: {message['content']}" for message in tail]
        summary = embedding_summary(middle)
        folded_text = "\n".join(lines_head + [summary] + lines_tail)

        if len(folded_text) > self.max_chars:
            head_text = "\n".join(lines_head)
            tail_text = "\n".join(lines_tail)
            reserved = len(head_text) + 1 + len(tail_text) + 1
            remaining = max(0, self.max_chars - reserved)
            if remaining >= len("[EMBEDDING_SUMMARY") and len(summary) > remaining:
                summary = summary[:remaining]
                folded_text = "\n".join(lines_head + [summary] + lines_tail)
            if len(folded_text) > self.max_chars:
                folded_text = self._collapse(folded_text, self.max_chars)

        return CompactResult(
            truncated=True,
            banner="TRUNCATED: embedding vector folding 80% threshold",
            text=folded_text,
        )

    @staticmethod
    def _collapse(text: str, max_chars: int | None = None) -> str:
        """保留首尾窗口，并在小预算下收缩窗口以适配 max_chars."""
        if not isinstance(text, str):
            logger.warning("_collapse coerced text from %s", type(text).__name__)
            text = str(text)
        if max_chars is None:
            if len(text) <= 900 + 500:
                return text
            return f"{text[:900]}\n[COLLAPSED head=900 tail=500]\n{text[-500:]}"

        try:
            budget = max(0, int(max_chars))
        except (ValueError, TypeError) as exc:
            logger.warning("invalid max_chars %r for _collapse: %s, using %d", max_chars, exc, len(text))
            budget = len(text)
        if budget == 0:
            # Avoid division by zero / empty output: return marker truncated
            marker = "[COLLAPSED head=0 tail=0]"
            return marker[: max(0, int(max_chars) if isinstance(max_chars, int) else 0)]
        if len(text) <= budget:
            return text

        head_len = min(900, len(text))
        tail_len = min(500, max(0, len(text) - head_len))
        if tail_len == 0 and len(text) > 1:
            tail_len = min(500, len(text) // 2)
            head_len = min(900, len(text) - tail_len)

        marker = f"[COLLAPSED head={head_len} tail={tail_len}]"
        while head_len + tail_len + len(marker) + 2 > budget:
            if head_len >= tail_len and head_len:
                head_len -= 1
            elif tail_len:
                tail_len -= 1
            else:
                return marker[:budget]
            marker = f"[COLLAPSED head={head_len} tail={tail_len}]"

        parts = []
        if head_len:
            parts.append(text[:head_len])
        parts.append(marker)
        if tail_len:
            parts.append(text[-tail_len:])
        result = "\n".join(parts)
        # Post-condition: guarantee len <= budget (fail-visible if violated)
        if len(result) > budget:
            logger.warning("_collapse budget violation: %d > %d, truncating", len(result), budget)
            result = result[:budget]
        return result

    def compact(self) -> CompactResult:
        """按 L1/L2/L3 阈值折叠上下文，保持既有 embedding summary 兼容."""
        text = self._render_messages(self._messages)
        total_chars = len(text)
        if total_chars <= self.max_chars * 0.5:
            return CompactResult(truncated=False, banner="OK", text=text)

        # Defensive copy for folding to avoid mutating original list via shared refs
        messages, microcompacted = self._microcompact(list(self._messages))
        working_text = self._render_messages(messages)
        total_chars = len(working_text)

        # L3 remains the existing vector folding path at the old 80% threshold.
        if total_chars > self.max_chars * 0.8:
            try:
                return self._embedding_compact(messages)
            except Exception as exc:
                logger.warning("embedding compact failed, falling back: %s", exc, exc_info=True)

        if total_chars > self.max_chars * 0.7:
            return CompactResult(
                truncated=True,
                banner="TRUNCATED: L2 context collapse head900 tail500",
                text=self._collapse(working_text, self.max_chars),
            )

        if microcompacted:
            return CompactResult(
                truncated=True,
                banner="TRUNCATED: L1 microcompact older tool results",
                text=working_text,
            )

        return CompactResult(truncated=False, banner="OK", text=text)

    def skills_digest(self, loader: "SkillsLoader") -> str:
        """首阶段：返回技能短摘要，用于上下文注入."""
        try:
            return loader.get_descriptions()
        except Exception as exc:
            logger.warning("skills_digest failed: %s", exc, exc_info=True)
            return ""

    def inject_skill_content(self, loader: "SkillsLoader", name: str) -> str:
        """二阶段：按需返回完整技能内容，包为 <skill_content>."""
        try:
            content = loader.get_content(name)
            safe_content = content.replace("</skill_content>", "&lt;/skill_content&gt;")
            safe_name = name.replace('"', "&quot;")
            return f"<skill_content name=\"{safe_name}\">\n{safe_content}\n</skill_content>"
        except Exception as exc:
            logger.warning("inject_skill_content failed for %r: %s", name, exc, exc_info=True)
            return ""

    def build_system_prompt(
        self,
        skill_count: int = 5,
        grounding_block: str = "",
        *,
        ledger=None,
        extra_rules: str = "",
        skills_digest: str | None = None,
        skills_loader=None,
        loader=None,
    ) -> str:
        """委托 prompt.build_system_prompt 组装 System Prompt，支持 Grounding 注入."""
        # Wave4: 透传 skills_digest via SkillsLoader.get_descriptions()
        _loader = skills_loader if skills_loader is not None else loader
        _digest = skills_digest
        if _digest is None:
            if _loader is not None:
                try:
                    _digest = _loader.get_descriptions()
                except Exception as exc:
                    logger.warning("loader.get_descriptions failed: %s", exc, exc_info=True)
                    _digest = ""
            else:
                try:
                    _digest = _cached_auto_digest()
                except Exception as exc:
                    logger.warning("auto digest failed: %s", exc, exc_info=True)
                    _digest = ""
        if _digest is None:
            _digest = ""
        try:
            from .prompt import build_system_prompt as _bsp

            return _bsp(
                skill_count=skill_count,
                grounding_block=grounding_block,
                ledger=ledger,
                skills_digest=_digest or "",
                extra_rules=extra_rules,
            )
        except Exception as exc:
            logger.warning("prompt.build_system_prompt fallback triggered: %s", exc, exc_info=True)
            block = grounding_block or (ledger.render_block() if ledger is not None and hasattr(ledger, "render_block") else "")
            rules = f"\n## Extra Rules\n{extra_rules}" if extra_rules else ""
            # include digest even in fallback for audit
            if _digest:
                return f"## Skills\n{_digest}\n## Grounding\n{block}{rules}\n## HARD RULE\nHARD RULE: Never quote price not in evidence."
            return f"## Grounding\n{block}{rules}\n## HARD RULE\nHARD RULE: Never quote price not in evidence."

    def get_system_prompt(
        self,
        skill_count: int = 5,
        grounding_block: str = "",
        *,
        ledger=None,
        extra_rules: str = "",
        skills_digest: str | None = None,
        skills_loader=None,
        loader=None,
    ) -> str:
        """build_system_prompt 的别名."""
        return self.build_system_prompt(skill_count=skill_count, grounding_block=grounding_block, ledger=ledger, extra_rules=extra_rules, skills_digest=skills_digest, skills_loader=skills_loader, loader=loader)


def skills_snapshot(loader: "SkillsLoader") -> str:
    """返回技能摘要快照，用于变更检测."""
    try:
        return loader.snapshot()
    except Exception as exc:
        logger.warning("skills snapshot failed: %s", exc, exc_info=True)
        return ""


def skills_observed_invalidate(loader: "SkillsLoader", path: str) -> None:
    """观察同步失效钩子，委托 loader 处理."""
    try:
        loader.observed_invalidate(path)
    except Exception as exc:
        logger.warning("observed_invalidate failed for %r: %s", path, exc, exc_info=True)
    try:
        _cached_auto_digest.cache_clear()
    except Exception as exc:
        logger.debug("cached auto digest clear failed: %s", exc)


def build_system_prompt(
    skill_count: int = 5,
    grounding_block: str = "",
    *,
    ledger=None,
    extra_rules: str = "",
    skills_digest: str | None = None,
    skills_loader=None,
    loader=None,
) -> str:
    """模块级便捷入口，委托 prompt.build_system_prompt."""
    _loader = skills_loader if skills_loader is not None else loader
    _digest = skills_digest
    if _digest is None:
        if _loader is not None:
            try:
                _digest = _loader.get_descriptions()
            except Exception as exc:
                logger.warning("loader.get_descriptions failed: %s", exc, exc_info=True)
                _digest = ""
        else:
            try:
                _digest = _cached_auto_digest()
            except Exception as exc:
                logger.warning("auto digest failed: %s", exc, exc_info=True)
                _digest = ""
    if _digest is None:
        _digest = ""
    try:
        from .prompt import build_system_prompt as _bsp

        return _bsp(
            skill_count=skill_count,
            grounding_block=grounding_block,
            ledger=ledger,
            skills_digest=_digest or "",
            extra_rules=extra_rules,
        )
    except Exception as exc:
        logger.warning("prompt.build_system_prompt fallback triggered: %s", exc, exc_info=True)
        block = grounding_block or (ledger.render_block() if ledger is not None and hasattr(ledger, "render_block") else "")
        rules = f"\n## Extra Rules\n{extra_rules}" if extra_rules else ""
        if _digest:
            return f"## Skills\n{_digest}\n## Grounding\n{block}{rules}\n## HARD RULE\nHARD RULE: Never quote price not in evidence."
        return f"## Grounding\n{block}{rules}\n## HARD RULE\nHARD RULE: Never quote price not in evidence."
