"""Research team graph — StateGraph(plan→execute→verify) + Subagents.

Minimal Wave B4 implementation:
- StateGraph with plan → execute → verify → END
- execute fans out to N create_agent leaf subagents (parallel placeholder)
- delegationDepth budget 5 (guard against recursive explosion)
- Uses Scope + tool contract placeholders (no hard deps)
"""

from __future__ import annotations

from typing import Dict, Any

try:
    from langgraph.graph import StateGraph, START, END
except Exception:  # pragma: no cover - fallback for older import path
    from langgraph.graph import StateGraph

    START = "__start__"  # type: ignore
    END = "__end__"  # type: ignore

from .state import State

# Budget for delegation depth — prevents infinite recursion
MAX_DELEGATION_DEPTH = 5

# --- Leaf subagent placeholder (create_agent semantics) ---
def _leaf_subagent(name: str):
    """Factory for leaf subagent node — mimics create_agent leaf."""
    def _run(state: State) -> Dict[str, Any]:
        depth = int(state.get("delegation_depth", 0))
        # Guard budget
        if depth >= MAX_DELEGATION_DEPTH:
            return {
                "messages": [{"role": "assistant", "content": f"{name}: delegation budget exceeded"}],
                "subagent_outputs": [{"agent": name, "status": "budget_exceeded"}],
            }
        # Simulate parallel research — no real LLM/tool call, just deterministic output
        return {
            "messages": [{"role": "assistant", "content": f"{name}: research done"}],
            "subagent_outputs": [{"agent": name, "output": f"{name} result"}],
            # delegation_depth not auto-incremented here; parent tracks
        }

    _run.__name__ = f"leaf_{name}"
    return _run


# --- Core nodes ---
def plan_node(state: State) -> Dict[str, Any]:
    """Plan phase — decompose query into subtasks."""
    msg = state.get("messages", [])
    last = msg[-1].get("content", "") if msg else ""
    plan_text = f"plan for: {last[:80]}" if last else "plan: default research"
    return {
        "messages": [{"role": "assistant", "content": "plan done"}],
        "plan": plan_text,
        "delegation_depth": int(state.get("delegation_depth", 0)),
    }


def execute_node(state: State) -> Dict[str, Any]:
    """Execute phase — fan-out to N leaf subagents in parallel (placeholder)."""
    depth = int(state.get("delegation_depth", 0))
    if depth >= MAX_DELEGATION_DEPTH:
        return {
            "messages": [{"role": "assistant", "content": "execute: budget exhausted"}],
        }
    # Simulate parallel subagents — call leaf factories inline
    # Real impl would use Send() API for true parallelism; minimal merges sequentially
    subagents = ["factor", "regime", "risk"]
    outputs = []
    msgs = []
    for name in subagents:
        leaf = _leaf_subagent(name)
        res = leaf(state)
        outputs.extend(res.get("subagent_outputs", []))
        msgs.extend(res.get("messages", []))
    # Also add execute marker
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
    """Build and compile the research team graph."""
    graph = StateGraph(State)
    graph.add_node("plan", plan_node)
    graph.add_node("execute", execute_node)
    graph.add_node("verify", verify_node)

    # plan → execute → verify → END
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
