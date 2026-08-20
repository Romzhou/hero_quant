"""Agent State — TypedDict with reducers for LangGraph research team."""

from __future__ import annotations

from typing import Annotated, TypedDict, List, Dict, Any


def _add_messages(left: List[Dict[str, Any]], right: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Reducer for messages — append."""
    if not isinstance(left, list):
        left = [left] if left else []
    if not isinstance(right, list):
        right = [right] if right else []
    return left + right


def _add_list(left: List[Any], right: List[Any]) -> List[Any]:
    if not isinstance(left, list):
        left = [left] if left else []
    if not isinstance(right, list):
        right = [right] if right else []
    return left + right


class State(TypedDict, total=False):
    """Research team state — plan→execute→verify."""

    messages: Annotated[List[Dict[str, Any]], _add_messages]
    delegation_depth: int
    plan: str
    intermediate_results: Annotated[List[Dict[str, Any]], _add_list]
    verification: str
    subagent_outputs: Annotated[List[Dict[str, Any]], _add_list]
