"""Research team graph — StateGraph(plan->execute->verify) + Subagents.

Minimal Wave B4 implementation:
- StateGraph with plan -> execute -> verify -> END
- execute fans out to N create_agent leaf subagents (parallel placeholder)
- delegationDepth budget 5 (guard against recursive explosion)
"""

from __future__ import annotations

from typing import Dict, Any

try:
    from langgraph.graph import StateGraph, START, END
except Exception:  # pragma: no cover - fallback for older import path
    from langgraph.graph import StateGraph  # type: ignore

    START = "__start__"  # type: ignore
    END = "__end__"  # type: ignore

# create_agent leaf semantics — optional import, fallback to placeholder
try:
    from langchain.agents import create_agent  # type: ignore  # LangChain 1.x
except Exception:
    try:
        from langgraph.prebuilt import create_react_agent as create_agent  # type: ignore
    except Exception:
        create_agent = None  # placeholder fallback

from .state import State

# Budget for delegation depth — prevents infinite recursion
MAX_DELEGATION_DEPTH = 5


def _leaf_subagent(name: str):
    """Factory for leaf subagent node — mimics create_agent leaf.

    In production this would be `create_agent(model, tools, system_prompt=...)`.
    Here we use deterministic placeholder to keep tests hermetic and parallel.
    """

    def _run(state: State) -> Dict[str, Any]:
        depth = int(state.get("delegation_depth", 0))
        if depth >= MAX_DELEGATION_DEPTH:
            return {
                "messages": [{"role": "assistant", "content": f"{name}: delegation budget exceeded"}],
                "subagent_outputs": [{"agent": name, "status": "budget_exceeded"}],
            }
        return {
            "messages": [{"role": "assistant", "content": f"{name}: research done"}],
            "subagent_outputs": [{"agent": name, "output": f"{name} result"}],
        }

    _run.__name__ = f"leaf_{name}"
    return _run


def plan_node(state: State) -> Dict[str, Any]:
    """Plan phase — decompose query into subtasks."""
    msgs = state.get("messages", [])
    last = msgs[-1].get("content", "") if msgs else ""
    plan_text = f"plan for: {last[:80]}" if last else "plan: default research"
    return {
        "messages": [{"role": "assistant", "content": "plan done"}],
        "plan": plan_text,
        "delegation_depth": int(state.get("delegation_depth", 0)),
    }


def execute_node(state: State) -> Dict[str, Any]:
    """Execute phase — fan-out to N leaf subagents in parallel (placeholder).

    Real impl would use Send() API for true parallelism:
        return Command(goto=[Send("leaf_factor", state), ...])
    Minimal merges sequentially to keep deterministic and testable.
    """
    depth = int(state.get("delegation_depth", 0))
    if depth >= MAX_DELEGATION_DEPTH:
        return {
            "messages": [{"role": "assistant", "content": "execute: budget exhausted"}],
        }
    subagents = ["factor", "regime", "risk"]
    outputs: list[Dict[str, Any]] = []
    msgs: list[Dict[str, Any]] = []
    for name in subagents:
        leaf = _leaf_subagent(name)
        res = leaf(state)
        outputs.extend(res.get("subagent_outputs", []))
        msgs.extend(res.get("messages", []))
    msgs.append({"role": "assistant", "content": "execute done"})
    return {
        "messages": msgs,
        "intermediate_results": outputs,
        "delegation_depth": depth + 1,
        "subagent_outputs": outputs,
    }


def verify_node(state: State) -> Dict[str, Any]:
    """Verify phase — grounding / risk check placeholder."""
    return {
        "messages": [{"role": "assistant", "content": "verify done"}],
        "verification": "verified",
    }


def build_research_graph():
    """Build and compile the research team graph (plan->execute->verify)."""
    graph = StateGraph(State)
    graph.add_node("plan", plan_node)
    graph.add_node("execute", execute_node)
    graph.add_node("verify", verify_node)

    try:
        graph.add_edge(START, "plan")
    except Exception:
        graph.set_entry_point("plan")  # fallback for older API
    graph.add_edge("plan", "execute")
    graph.add_edge("execute", "verify")
    graph.add_edge("verify", END)

    # Compile — no checkpointer for Wave B4 (PostgresSaver in C5)
    compiled = graph.compile()
    return compiled
