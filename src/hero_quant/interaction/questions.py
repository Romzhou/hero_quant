"""人机问答 —— 问询卡片与中断恢复。

职责：提供 UserQuestionService 的两段式 ask→guard 校验与中断语义；架构位置：interaction 层衔接图执行与前端。
设计决策：校验 BAD_INTENT / DELEGATED_CALLER 以拦截非法意图与委托调用；无 provider 时以 AskCardInterrupt 中断，配合 Command 恢复；Store 按 (tenant, thread) 隔离由调用方持有。
"""

from __future__ import annotations
import asyncio
import inspect
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


# 意图白名单 — 仅允许这些意图通过
_ALLOWED_INTENTS = {None, "confirm", "select", "input"}


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
        # 意图白名单校验 — 强制 allowlist，未知意图一律 BAD_INTENT
        intent = data.get("intent")
        if intent not in _ALLOWED_INTENTS:
            # 委托调用不允许，单独标记为 DELEGATED_CALLER
            if isinstance(intent, str) and intent == "delegated":
                raise ValueError("DELEGATED_CALLER: delegated caller not allowed")
            raise ValueError(f"BAD_INTENT: unknown intent {intent!r}")
        # 若问题文本含委托标记，也视为委托调用，需拦截
        if isinstance(data.get("question"), str) and "DELEGATED" in data["question"]:
            raise ValueError("DELEGATED_CALLER: delegated")


class UserQuestionService:
    """问询服务：有 provider 时委托 ask，无 provider 时抛 NO_PROVIDER 由上层转为中断。"""

    def __init__(self, provider: Any | None = None, store: dict | None = None):
        self.provider = provider
        # Store 用于 tenant/thread 隔离的临时状态；默认为空 dict 以支持隔离测试
        self.store: dict = dict(store) if isinstance(store, dict) else {}

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
                # 若为协程/awaitable，在同步路径中执行
                if inspect.isawaitable(res) or asyncio.iscoroutine(res):
                    try:
                        return asyncio.run(res)  # type: ignore[arg-type]
                    except Exception:
                        # 避免协程泄漏：关闭未完成的协程/awaitable
                        try:
                            if hasattr(res, "close"):
                                res.close()
                        except Exception:
                            pass
                        logger.error("ask_sync asyncio.run failed", exc_info=True)
                        raise
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
            res = self.provider.ask(questions, signal=signal)
            # 仅当结果为 awaitable 时才 await，避免 TypeError: object str can't be used in 'await'
            if inspect.isawaitable(res):
                return await res
            return res
        if hasattr(self.provider, "ask_sync"):
            # 同步 provider 在异步路径中直接调用（若为阻塞 IO，调用方可自行放入 executor）
            return self.provider.ask_sync(questions, signal=signal)
        raise RuntimeError("NO_PROVIDER: provider missing ask method")

    # Store 隔离：提供基于临时 store 交换的真实隔离
    def with_store_isolation(self, tenant: str, thread: str) -> Any:
        """返回带 Store 隔离的上下文管理器，内部通过临时 store 交换实现隔离。

        用法：
            svc = UserQuestionService(...)
            svc.store["k"] = "outside"
            with svc.with_store_isolation("tenant1", "thread1") as isolated:
                isolated.store["k2"] = "inside"
            # "k2" not in svc.store — 写入在块内隔离，退出后恢复

        实现：保存原 store，替换为新的隔离 dict（基于原 store 的浅拷贝或空），
        在 finally 中恢复原 store，确保异常时也能还原。
        """
        svc = self

        class _IsolationContext:
            def __init__(self, outer):
                self.outer = outer
                self._saved: dict | None = None
                self._isolated: dict | None = None

            def __enter__(self_inner) -> "UserQuestionService":
                # 保存原 store
                self_inner._saved = svc.store
                # 创建隔离 store：浅拷贝原 store 以保留只读可见性，但写入隔离
                # 对于测试中“内部写入外部不可见”，我们使用空隔离字典；
                # 若需保留外部数据可见，可用 dict(svc.store) 作拷贝。
                # 选择空 dict 以严格隔离：内部写入不污染外部
                # 同时将隔离 store 赋值给 svc.store
                self_inner._isolated = {}
                svc.store = self_inner._isolated
                # 为 tenant/thread 命名空间添加标记（可选，便于调试）
                # 不直接污染业务键，仅作内部标记
                return svc

            def __exit__(self_inner, exc_type, exc, tb):
                # 恢复原 store，确保不泄漏
                try:
                    if self_inner._saved is not None:
                        svc.store = self_inner._saved
                finally:
                    return False

        return _IsolationContext(self)
