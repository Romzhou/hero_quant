"""研究团队共享状态：LangGraph StateGraph 的 TypedDict 与归约器。

职责：定义 plan→execute→verify 流水线中跨节点的共享字段与合并语义。
架构位置：agent 层状态契约，被 graph/build_research_graph 与各节点读写。
关键设计：
- 消息与列表字段采用 append 归约（Annotated + reducer），保多路并行合并确定性
- delegation_depth 计数限 5，配合熔断防无限委派
- 轻量 pros/cons/confidence 由 verify 单次综合写入，供下游校验
"""

from __future__ import annotations

from typing import Annotated, TypedDict, List, Dict, Any


def _add_messages(left: List[Dict[str, Any]], right: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """消息归约：append 合并，兼容非列表输入."""
    if not isinstance(left, list):
        left = [left] if left else []
    if not isinstance(right, list):
        right = [right] if right else []
    return left + right


def _add_list(left: List[Any], right: List[Any]) -> List[Any]:
    """通用列表归约：append 合并."""
    if not isinstance(left, list):
        left = [left] if left else []
    if not isinstance(right, list):
        right = [right] if right else []
    return left + right


class State(TypedDict, total=False):
    """研究团队状态，承载 plan→execute→verify 全流程共享数据."""

    messages: Annotated[List[Dict[str, Any]], _add_messages]
    delegation_depth: int
    plan: str
    intermediate_results: Annotated[List[Dict[str, Any]], _add_list]
    verification: str
    subagent_outputs: Annotated[List[Dict[str, Any]], _add_list]
    pros: Annotated[List[str], _add_list]
    cons: Annotated[List[str], _add_list]
    confidence: float
