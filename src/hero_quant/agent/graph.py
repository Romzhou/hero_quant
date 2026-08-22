"""研究团队调度图：StateGraph 编排 plan → 并行分析师 → verify。

职责：将单轮研究请求分解为多分析师并行子任务并做轻量综合校验。
架构位置：agent 层上层编排，基于 LangGraph StateGraph，State 为共享状态与归约容器。
关键设计：
- 真并行扇出：plan 节点返回 Command(goto=[Send(...)]) 驱动多 analyst 并发
- 归约合并：verify 通过 Annotated[list, add] 归约多路输出，delegationDepth 限 5 防递归
- 容错与预算：RetryPolicy/error_handler 的 Saga 补偿占位，BudgetBreaker 做成本熔断
"""

from __future__ import annotations

from typing import Dict, Any, List

try:
    from langgraph.graph import StateGraph, START, END
except Exception:  # pragma: no cover - fallback for older import path
    from langgraph.graph import StateGraph  # type: ignore

    START = "__start__"  # type: ignore
    END = "__end__"  # type: ignore

# Send/Command 扇出原语：优先 langgraph.types，回落 graph
try:
    from langgraph.types import Command, Send  # type: ignore
except Exception:
    try:
        from langgraph.graph import Command, Send  # type: ignore
    except Exception:
        Command = None  # type: ignore
        Send = None  # type: ignore

# 叶节点语义：优先 LangChain create_agent，回落占位
try:
    from langchain.agents import create_agent  # type: ignore  # LangChain 1.x
except Exception:
    try:
        from langgraph.prebuilt import create_react_agent as create_agent  # type: ignore
    except Exception:
        create_agent = None  # type: ignore

from .state import State

# 策略占位：优雅降级与成本熔断，按需导入
try:
    from .policies import BudgetBreaker, RetryPolicy, error_handler  # type: ignore
except Exception:  # pragma: no cover
    BudgetBreaker = RetryPolicy = error_handler = None  # type: ignore

# 委派深度上限，防无限递归
MAX_DELEGATION_DEPTH = 5

# 全局成本熔断器（滑动窗口）占位
_breaker = None
try:
    if BudgetBreaker is not None:
        _breaker = BudgetBreaker(daily_limit=5.0)
except Exception:
    _breaker = None

# 分析师正规范畴与别名归一
_CANONICAL = ["market", "sentiment", "news", "fundamentals", "factor", "regime", "risk"]
_ALIAS_MAP = {
    "market": "market",
    "sentiment": "sentiment",
    "social": "sentiment",
    "news": "news",
    "fundamentals": "fundamentals",
    "fundamental": "fundamentals",
    "factor": "factor",
    "regime": "regime",
    "risk": "risk",
}


def _resolve_targets_from_text(text: str) -> List[str]:
    """从自由文本推断需扇出的分析师目标."""
    low = (text or "").lower()
    out: List[str] = []
    for kw, node in [
        ("market", "market"),
        ("sentiment", "sentiment"),
        ("social", "sentiment"),
        ("news", "news"),
        ("fundamental", "fundamentals"),
        ("factor", "factor"),
        ("regime", "regime"),
        ("risk", "risk"),
    ]:
        if kw in low and node not in out:
            out.append(node)
    return out


def _normalize_selected(selected: List[str] | None) -> List[str]:
    if selected is None:
        return []
    norm: List[str] = []
    for s in selected:
        key = s.strip().lower()
        canon = _ALIAS_MAP.get(key, key)
        if canon not in norm:
            norm.append(canon)
    return norm


def _leaf_subagent(name: str):
    """创建叶分析师节点，占位实现 create_agent 叶语义。"""

    def _run(state: State) -> Dict[str, Any]:
        depth = int(state.get("delegation_depth", 0))
        if depth >= MAX_DELEGATION_DEPTH:
            return {
                "messages": [{"role": "assistant", "content": f"{name}: delegation budget exceeded"}],
                "subagent_outputs": [{"agent": name, "status": "budget_exceeded"}],
            }
        # 成本熔断占位：按固定成本探询是否需降级
        if _breaker is not None:
            try:
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


def _lazy_command_send():
    """懒加载 Command/Send，避免模块导入时拖慢启动."""
    try:
        from langgraph.types import Command as _C, Send as _S  # type: ignore

        return _C, _S
    except Exception:
        try:
            from langgraph.graph import Command as _C2, Send as _S2  # type: ignore

            return _C2, _S2
        except Exception:
            return Command, Send


def plan_node(state: State):
    """计划阶段：分解任务并通过 Send 扇出实现并行调度，超委派深度则直接返回预算提示."""
    depth = int(state.get("delegation_depth", 0))
    if depth >= MAX_DELEGATION_DEPTH:
        return {
            "messages": [{"role": "assistant", "content": "plan: delegation budget exceeded"}],
            "delegation_depth": depth,
        }
    msgs = state.get("messages", [])
    last = ""
    try:
        if msgs and isinstance(msgs[-1], dict):
            last = msgs[-1].get("content", "") or ""
        elif msgs:
            last = str(msgs[-1])
    except Exception:
        last = ""
    plan_text_src = state.get("plan", "") or ""
    combined = f"{plan_text_src} {last}"
    targets = _resolve_targets_from_text(combined)
    if not targets:
        targets = ["market", "sentiment", "news"]
    plan_text = f"plan for: {last[:80]}" if last else "plan: default research"
    Cmd, Snd = _lazy_command_send()
    if Cmd is None or Snd is None:
        return {
            "messages": [{"role": "assistant", "content": "plan done"}],
            "plan": plan_text,
            "delegation_depth": depth,
        }
    return Cmd(
        update={
            "messages": [{"role": "assistant", "content": "plan done"}],
            "plan": plan_text,
            "delegation_depth": depth,
        },
        goto=[Snd(t, state) for t in targets],
    )


def execute_node(state: State) -> Dict[str, Any]:
    """执行阶段：旧式串行扇出，保留兼容；新图已由 plan→Send 直连并行."""
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
        try:
            res = leaf(state)
        except Exception as e:
            if error_handler is not None:
                try:
                    _ = error_handler(state, e)
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


# 轻量多空对抗 prompt：单次综合，避免多轮风险链
_VERIFY_PROMPT = "请给出多空两面 pros/cons + 置信度"


def verify_node(state: State) -> Dict[str, Any]:
    """校验阶段：汇总子代理输出做 pros/cons 多空综合与置信度合成."""
    prompt = _VERIFY_PROMPT  # noqa: F841

    outputs = state.get("subagent_outputs") or state.get("intermediate_results") or []
    n = len(outputs) if isinstance(outputs, list) else 0
    confidence = round(min(0.85, 0.55 + 0.05 * max(1, n)), 2) if n else 0.65
    pros = [
        "多头: 趋势/动量延续或估值修复预期",
        "pros: positive momentum / sentiment support",
    ]
    cons = [
        "空头: 回撤/波动或基本面证伪风险",
        "cons: pullback risk / valuation overhang",
    ]
    verification = f"pros:{pros} cons:{cons} confidence:{confidence} | {prompt}"
    return {
        "messages": [{"role": "assistant", "content": verification}],
        "verification": verification,
        "pros": pros,
        "cons": cons,
        "confidence": confidence,
    }


def compensate_node(state: State) -> Dict[str, Any]:
    """Saga 补偿节点：回滚占位."""
    return {
        "messages": [{"role": "assistant", "content": "compensate done"}],
        "verification": "compensated",
    }


def build_research_graph(selected: List[str] | None = None):
    """构建并编译研究团队图，selected 为空时默认扇出 market/sentiment/news."""
    normalized = _normalize_selected(selected) if selected is not None else ["market", "sentiment", "news"]

    graph = StateGraph(State)

    def _plan(state: State):
        depth = int(state.get("delegation_depth", 0))
        if depth >= MAX_DELEGATION_DEPTH:
            return {
                "messages": [{"role": "assistant", "content": "plan: delegation budget exceeded"}],
                "delegation_depth": depth,
            }
        targets = normalized if normalized else ["market", "sentiment", "news"]
        msgs = state.get("messages", [])
        last = ""
        try:
            if msgs and isinstance(msgs[-1], dict):
                last = msgs[-1].get("content", "") or ""
            elif msgs:
                last = str(msgs[-1])
        except Exception:
            last = ""
        plan_text = f"plan for: {last[:80]}" if last else "plan: default research"
        Cmd, Snd = _lazy_command_send()
        if Cmd is None or Snd is None:
            return {
                "messages": [{"role": "assistant", "content": "plan done"}],
                "plan": plan_text,
                "delegation_depth": depth,
            }
        return Cmd(
            update={
                "messages": [{"role": "assistant", "content": "plan done"}],
                "plan": plan_text,
                "delegation_depth": depth,
            },
            goto=[Snd(t, state) for t in targets],
        )

    _plan.__name__ = "plan"
    graph.add_node("plan", _plan)

    # 注册全部叶节点，避免 Send 目标缺失；执行边均汇至 verify
    all_nodes = set(normalized) | set(_CANONICAL)
    for name in all_nodes:
        graph.add_node(name, _leaf_subagent(name))
        graph.add_edge(name, "verify")

    graph.add_node("execute", execute_node)
    graph.add_node("verify", verify_node)
    graph.add_node("compensate", compensate_node)

    try:
        graph.add_edge(START, "plan")
    except Exception:
        graph.set_entry_point("plan")
    graph.add_edge("verify", END)
    graph.add_edge("compensate", END)

    compiled = graph.compile()
    return compiled
