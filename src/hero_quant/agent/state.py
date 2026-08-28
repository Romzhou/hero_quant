"""研究团队共享状态：LangGraph StateGraph 的 TypedDict 与归约器。

职责：定义 plan→execute→verify 流水线中跨节点的共享字段与合并语义。
架构位置：agent 层状态契约，被 graph/build_research_graph 与各节点读写。
关键设计：
- 消息与列表字段采用 append 归约（Annotated + reducer），保多路并行合并确定性
- delegation_depth 计数限 5，配合熔断防无限委派
- 轻量 pros/cons/confidence 由 verify 单次综合写入，供下游校验
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, TypedDict


def _add_messages(left: List[Dict[str, Any]] | Dict[str, Any] | None, right: List[Dict[str, Any]] | Dict[str, Any] | None) -> List[Dict[str, Any]]:
    """消息归约：append 合并，兼容非列表输入。None 显式判空，空 dict 保留."""
    if left is None:
        left_list: List[Dict[str, Any]] = []
    elif isinstance(left, list):
        left_list = left
    else:
        left_list = [left]  # type: ignore[list-item]
    if right is None:
        right_list: List[Dict[str, Any]] = []
    elif isinstance(right, list):
        right_list = right
    else:
        right_list = [right]  # type: ignore[list-item]
    return left_list + right_list


def _add_list(left: List[Any] | Any | None, right: List[Any] | Any | None) -> List[Any]:
    """通用列表归约：append 合并。None 显式判空，非 list 包 [x]."""
    if left is None:
        left_list: List[Any] = []
    elif isinstance(left, list):
        left_list = left
    else:
        left_list = [left]
    if right is None:
        right_list: List[Any] = []
    elif isinstance(right, list):
        right_list = right
    else:
        right_list = [right]
    return left_list + right_list


def _keep_last(a: Any, b: Any) -> Any:
    """保留最后非 None 值归约：b 非 None 则取 b，否则取 a."""
    return b if b is not None else a


def _max_depth(a: int | None, b: int | None) -> int | None:
    """delegation_depth 归约：取最大值，None 视为缺省."""
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


class State(TypedDict, total=False):
    """研究团队状态，承载 plan→execute→verify 全流程共享数据。

    Invariants:
    - delegation_depth in 0..5 (MAX_DELEGATION_DEPTH=5，超限熔断)
    - confidence in 0..1 (verify 合成置信度，0..1 归一)
    """

    messages: Annotated[List[Dict[str, Any]], _add_messages]
    delegation_depth: Annotated[int, _max_depth]
    plan: Annotated[str, _keep_last]
    intermediate_results: Annotated[List[Dict[str, Any]], _add_list]
    verification: Annotated[str, _keep_last]
    subagent_outputs: Annotated[List[Dict[str, Any]], _add_list]
    pros: Annotated[List[str], _add_list]
    cons: Annotated[List[str], _add_list]
    confidence: Annotated[float, _keep_last]
