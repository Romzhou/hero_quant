"""Research team graph — StateGraph(plan->execute->verify) + Subagents + Saga.

Wave B4 + C3:
- StateGraph with plan -> execute -> verify -> END
- execute fans out to N create_agent leaf subagents (parallel placeholder)
- delegationDepth budget 5
- Wave C3: RetryPolicy + error_handler Saga compensate + BudgetBreaker placeholder
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

# Wave C3 policies — graceful degradation + cost breaker (import placeholder, not hard dep)
try:
    from .policies import BudgetBreaker, RetryPolicy, error_handler  # type: ignore
except Exception:  # pragma: no cover
    BudgetBreaker = RetryPolicy = error_handler = None  # type: ignore

# Budget for delegation depth — prevents infinite recursion
MAX_DELEGATION_DEPTH = 5

# Global breaker placeholder (sliding window)
_breaker = None
try:
    if BudgetBreaker is not None:
        _breaker = BudgetBreaker(daily_limit=5.0)
except Exception:
    _breaker = None


def _leaf_subagent(name: str):
    """Factory for leaf subagent node — mimics create_agent leaf."""

    def _run(state: State) -> Dict[str, Any]:
        depth = int(state.get("delegation_depth", 0))
        if depth >= MAX_DELEGATION_DEPTH:
            return {
                "messages": [{"role": "assistant", "content": f"{name}: delegation budget exceeded"}],
                "subagent_outputs": [{"agent": name, "status": "budget_exceeded"}],
            }
        # BudgetBreaker check placeholder — if cost too high, fallback
        # (cost not tracked in state minimal; just consult breaker)
        if _breaker is not None:
            try:
                # placeholder cost 0.1 per leaf; check fallback
                if _breaker.should_fallback(cost=0.1):
                    return {
                        "messages": [{"role": "assistant", "content": f"{name}: budget fallback"}],
                        "subagent_outputs": [{"agent": name, "status": "fallback"}],
                    }
            except Exception:
                pass
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
    Also wraps retry placeholder via RetryPolicy.
    """
    depth = int(state.get("delegation_depth", 0))
    if depth >= MAX_DELEGATION_DEPTH:
        return {
            "messages": [{"role": "assistant", "content": "execute: budget exhausted"}],
        }
    # RetryPolicy placeholder — not actually retrying here, just shows error_handler path
    subagents = ["factor", "regime", "risk"]
    outputs: list[Dict[str, Any]] = []
    msgs: list[Dict[str, Any]] = []
    for name in subagents:
        leaf = _leaf_subagent(name)
        try:
            res = leaf(state)
        except Exception as e:
            # Saga error_handler → compensate
            if error_handler is not None:
                try:
                    cmd = error_handler(state, e)
                    # In real graph, would return Command(goto="compensate")
                    # Minimal: record error and continue
                    msgs.append({"role": "assistant", "content": f"{name}: error {e} -> compensate"})
                    continue
                except Exception:
                    pass
            res = {"messages": [{"role": "assistant", "content": f"{name}: error"}], "subagent_outputs": []}
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


def compensate_node(state: State) -> Dict[str, Any]:
    """Saga compensation — rollback placeholder."""
    return {
        "messages": [{"role": "assistant", "content": "compensate done"}],
        "verification": "compensated",
    }


def build_research_graph():
    """Build and compile the research team graph (plan->execute->verify [+compensate])."""
    graph = StateGraph(State)
    graph.add_node("plan", plan_node)
    graph.add_node("execute", execute_node)
    graph.add_node("verify", verify_node)
    graph.add_node("compensate", compensate_node)

    try:
        graph.add_edge(START, "plan")
    except Exception:
        graph.set_entry_point("plan")  # fallback for older API
    graph.add_edge("plan", "execute")
    graph.add_edge("execute", "verify")
    graph.add_edge("verify", END)
    # compensate is reachable via error_handler Command goto (Saga) — keep isolated for minimal
    graph.add_edge("compensate", END)

    # Compile — no checkpointer for Wave B4 (PostgresSaver in C5)
    compiled = graph.compile()
    return compiled
