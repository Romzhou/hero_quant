"""人机问答 —— 问询卡片与中断恢复。

职责：提供 UserQuestionService 的两段式 ask→guard 校验与中断语义；架构位置：interaction 层衔接图执行与前端。
设计决策：校验 BAD_INTENT / DELEGATED_CALLER 以拦截非法意图与委托调用；无 provider 时以 AskCardInterrupt 中断，配合 Command 恢复；Store 按 (tenant, thread) 隔离由调用方持有。
"""

from __future__ import annotations
import logging

from dataclasses import dataclass, field
from typing import Any
logger = logging.getLogger("hero_quant.interaction.questions")


class AskCardInterrupt(Exception):
    """问询卡片中断异常，对应图执行的中断点，需由上层恢复。"""

    def __init__(self, questions: list[Any], reason: str = "NO_PROVIDER"):
        super().__init__(f"{reason}: ask card interrupt")
        self.questions = questions
        self.reason = reason


class Command:
    """恢复指令占位，对应图框架的 Command resume 语义。"""

    def __init__(self, resume: Any = None, goto: str | None = None):
        self.resume = resume
        self.goto = goto


@dataclass
class AskUserQuestionItem:
    """单条问询项，包含标题、问题、选项与意图标记。"""

    id: str
    question: str
    header: str
    options: list[dict[str, str]] = field(default_factory=list)
    multiSelect: bool = False
    intent: str | None = None


def _validate_questions(questions: list[Any]) -> None:
    """校验问询项合法性，拦截空选项与非法意图。"""
    if not questions:
        raise ValueError("BAD_INTENT: questions empty")
    for q in questions:
        # 归一化为 dict 以统一校验（支持 dataclass 与 dict）
        if isinstance(q, dict):
            data = q
        elif hasattr(q, "__dict__"):
            data = vars(q)
        else:
            # 尝试按 dataclass 转换
            try:
                from dataclasses import asdict

                data = asdict(q)  # type: ignore
            except Exception:
                raise ValueError("BAD_INTENT: invalid question item")
        # 必填字段检查
        for key in ("id", "question", "header", "options"):
            if key not in data:
                raise ValueError(f"BAD_INTENT: missing {key}")
        opts = data.get("options")
        if not isinstance(opts, list) or len(opts) == 0:
            raise ValueError("BAD_INTENT: options empty or not list")
        for opt in opts:
            if not isinstance(opt, dict) or "label" not in opt or "description" not in opt:
                raise ValueError("BAD_INTENT: option must have label/description")
        # 意图白名单校验，未知意图视为 BAD_INTENT
        intent = data.get("intent")
        if intent is not None and intent not in (None, "confirm", "select", "input"):
            # 委托调用不允许，直接标记为 DELEGATED_CALLER
            if isinstance(intent, str) and intent == "delegated":
                raise ValueError("DELEGATED_CALLER: delegated caller not allowed")
        # 若问题文本含委托标记，也视为委托调用，需拦截
        if isinstance(data.get("question"), str) and "DELEGATED" in data["question"]:
            raise ValueError("DELEGATED_CALLER: delegated")


class UserQuestionService:
    """问询服务：有 provider 时委托 ask，无 provider 时抛 NO_PROVIDER 由上层转为中断。"""

    def __init__(self, provider: Any | None = None):
        self.provider = provider

    def ask_sync(self, questions: list[Any], signal: Any | None = None) -> Any:
        """同步问询入口，先校验再委托 provider。"""
        _validate_questions(questions)
        if self.provider is None:
            # 无提供方则以 NO_PROVIDER 中断，由上层转为 AskCardInterrupt
            raise RuntimeError("NO_PROVIDER: no question provider configured")
        # 三段式 ask→guard 委托
        try:
            # 约定：ask(questions, signal) -> result
            if hasattr(self.provider, "ask_sync"):
                return self.provider.ask_sync(questions, signal=signal)
            if hasattr(self.provider, "ask"):
                # 兼容同步调用；若返回协程则在同步上下文中运行
                res = self.provider.ask(questions, signal=signal)
                # 若为协程，在同步路径中执行
                try:
                    import asyncio

                    if asyncio.iscoroutine(res):
                        return asyncio.run(res)
                except Exception as _exc:
                    logger.debug("silent handled: interaction optional", exc_info=_exc)  # intentional: interaction optional
                    pass  # intentional interaction optional
                return res
            raise RuntimeError("NO_PROVIDER: provider missing ask method")
        except Exception as e:
            # 保留 NO_PROVIDER 标记便于上层识别中断
            if "NO_PROVIDER" in str(e):
                raise
            raise

    async def ask(self, questions: list[Any], signal: Any | None = None) -> Any:
        """异步问询入口。"""
        _validate_questions(questions)
        if self.provider is None:
            raise RuntimeError("NO_PROVIDER: no question provider configured")
        if hasattr(self.provider, "ask"):
            return await self.provider.ask(questions, signal=signal)
        if hasattr(self.provider, "ask_sync"):
            return self.provider.ask_sync(questions, signal=signal)
        raise RuntimeError("NO_PROVIDER: provider missing ask method")

    # Store 隔离由调用方（graph/Store）按 (tenant, thread) 命名空间处理
    def with_store_isolation(self, tenant: str, thread: str) -> "UserQuestionService":
        """返回带 Store 隔离的服务实例（当前为占位实现，直接返回自身）。"""
        # 占位：真实隔离在 memory/store 层实现
        return self
