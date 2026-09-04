"""Agent 容器：AgentState + AgentContainer（幂等初始化 + PG 持久复用）。

职责：承载单轮问答的 Agent 状态契约与 LLM/Graph/Checkpointer 依赖容器，
供 lifespan 经 inject_agent_container 注入 app.state.agent。
架构位置：agent 层容器（因 agent/graph.py 已为文件存在，落位为
agent/container.py 以避包/文件冲突）。
关键设计：模块级 _init_lock/_graph_lock 保懒加载；init_graph 幂等；
checkpointer 复用 HERO_CHECKPOINT_DSN 经 checkpoint.postgres.get_saver
（memory:// 兜底离线可用，永不阻断初始化）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

_init_lock = asyncio.Lock()
_graph_lock = asyncio.Lock()


class AgentState(BaseModel):
    """Agent 状态"""

    # 输入
    user_input: str = ""

    # 上下文
    session_id: int = 0
    user_id: int = 0

    # 对话历史
    messages: List[Any] = []

    # LLM 输出
    llm_response: str = ""

    # Tool 调用
    tool_calls: List[Any] = []
    tool_results: List[Any] = []

    # 最终响应
    final_response: str = ""

    # 元数据
    iterations: int = 0
    max_iterations: int = 5

    model_config = ConfigDict(arbitrary_types_allowed=True)


def _build_agent_graph(checkpointer: Any = None):
    """构建最小 think->respond 图；checkpointer 兼容则挂载，否则裸编译。"""
    from langgraph.graph import END, StateGraph

    def _think(state: AgentState) -> dict:
        text = state.user_input or ""
        return {"llm_response": text, "iterations": int(state.iterations or 0) + 1}

    def _respond(state: AgentState) -> dict:
        return {"final_response": state.llm_response or state.user_input or ""}

    graph = StateGraph(AgentState)
    graph.add_node("think", _think)
    graph.add_node("respond", _respond)
    graph.set_entry_point("think")
    graph.add_edge("think", "respond")
    graph.add_edge("respond", END)
    if checkpointer is not None:
        try:
            return graph.compile(checkpointer=checkpointer)
        except Exception as exc:
            logger.debug("graph compile with checkpointer failed, fallback bare: %s", exc)
    return graph.compile()


class AgentContainer:
    """Agent 依赖容器：持有 llm/graph/checkpointer，lifespan 注入 app.state。"""

    def __init__(self):
        self.llm: Optional[Any] = None
        self.graph: Optional[Any] = None
        self.checkpointer: Optional[Any] = None

    def init_llm(self, llm: Optional[Any] = None) -> Optional[Any]:
        """初始化 LLM（显式注入优先，否则经 LLMFactory 离线友好创建）。"""
        if llm is not None:
            self.llm = llm
            return self.llm
        if self.llm is not None:
            return self.llm
        try:
            from hero_quant.llm.factory import LLMFactory

            self.llm = LLMFactory().create()
        except Exception as exc:
            logger.warning("agent llm init failed, keep None: %s", exc)
            self.llm = None
        return self.llm

    def init_checkpointer(self, dsn: Optional[str] = None) -> Optional[Any]:
        """复用 HERO_CHECKPOINT_DSN 的 PG 持久；失败回退 None 永不抛异常。"""
        if self.checkpointer is not None:
            return self.checkpointer
        eff = (dsn or os.environ.get("HERO_CHECKPOINT_DSN", "") or "").strip() or "memory://default"
        try:
            from hero_quant.checkpoint.postgres import get_saver

            self.checkpointer = get_saver(eff)
        except Exception as exc:
            logger.debug("agent checkpointer init failed: %s", exc)
            self.checkpointer = None
        return self.checkpointer

    def init_graph(self) -> Any:
        """初始化 Agent Graph（幂等：重复调用返回同一编译产物）。"""
        if self.graph is not None:
            return self.graph
        try:
            self.init_checkpointer()
        except Exception:
            pass
        self.graph = _build_agent_graph(self.checkpointer)
        logger.info("agent graph compiled")
        return self.graph


def inject_agent_container(app, container: AgentContainer | None = None) -> AgentContainer:
    """lifespan 注入点：把 AgentContainer 挂到 app.state.agent（不改现有 lifespan 本体时可单独调用）。"""
    bound = container or AgentContainer()
    app.state.agent = bound
    return bound


__all__ = ["AgentContainer", "AgentState", "_graph_lock", "_init_lock", "inject_agent_container"]
