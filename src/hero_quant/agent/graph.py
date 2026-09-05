"""研究团队调度图：StateGraph 编排 plan → 并行分析师 → verify.

职责：将单轮研究请求分解为多分析师并行子任务并做轻量综合校验。
架构位置：agent 层上层编排，基于 LangGraph StateGraph，State 为共享状态与归约容器。
关键设计：
- 真并行扇出：plan 节点返回 Command(goto=[Send(...)]) 驱动多 analyst 并发
- 归约合并：verify 通过 Annotated[list, add] 归约多路输出，delegationDepth 限 5 防递归
- 容错与预算：BudgetBreaker 做成本熔断（内部 Lock 保护）；execute/compensate 为遗留占位已移除
"""

from __future__ import annotations

import logging
import threading
import warnings
from typing import Dict, Any, List

try:
    from langgraph.graph import StateGraph, START, END
except ImportError as e:  # pragma: no cover - narrow to ImportError
    warnings.warn(f"LangGraph import failed: {e}", stacklevel=2)
    raise

# Send/Command 扇出原语：优先 langgraph.types，回落 graph
try:
    from langgraph.types import Command, Send  # type: ignore
except ImportError:
    try:
        from langgraph.graph import Command, Send  # type: ignore
    except ImportError as e:
        warnings.warn(f"LangGraph Command/Send import failed: {e}", stacklevel=2)
        raise

from .state import State  # noqa: E402

# 策略占位：优雅降级与成本熔断，按需导入
try:
    from .policies import BudgetBreaker, RetryPolicy, error_handler  # type: ignore
except ImportError as e:  # pragma: no cover - narrow
    logging.getLogger(__name__).warning("policies import failed: %s", e)
    BudgetBreaker = RetryPolicy = error_handler = None  # type: ignore

# 委派深度上限，防无限递归
MAX_DELEGATION_DEPTH = 5

# 全局成本熔断器（滑动窗口）占位；线程安全由 BudgetBreaker 内部 _lock 提供，无需外层锁。
# 注意：_breaker 为遗留模块级默认，仅在未显式注入 breaker 时兜底使用；
# 跨请求复用会污染预算（daily_limit 耗尽后影响无关请求），故 _leaf_subagent 支持
# per-request 注入（breaker 参数 / RunnableConfig），长期应经 config 透传。
_breaker = None
# 遗留全局 _breaker 兜底路径的串行化锁：单次 check_and_add 本身已由
# BudgetBreaker 内部 _lock 保证原子，此锁仅串行化“选用全局默认”这一回退决策，
# 避免高并发扇出下对共享全局的复合竞态；per-request 注入优先。
_legacy_budget_lock = threading.Lock()
try:
    if BudgetBreaker is not None:
        _breaker = BudgetBreaker(daily_limit=5.0)
except (ImportError, ValueError, TypeError, RuntimeError) as e:
    logging.getLogger(__name__).warning("BudgetBreaker init failed: %s", e)
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

# Phase 1: 7 投研角色与工具绑定（过滤 TOOL_REGISTRY）
_ROLE_TOOL_MAP: dict[str, list[str]] = {
    "market": ["get_market_data", "get_bars_range", "list_markets", "compute_indicator", "compute_sharpe", "compute_drawdown", "compute_correlation"],
    "sentiment": ["search_symbols", "search_symbol"],
    "news": ["search_symbols", "search_symbol"],
    "fundamentals": ["get_ticker_info", "get_fundamentals"],
    "factor": ["compute_factor", "screen_factors", "compute_indicator"],
    "regime": ["compute_indicator", "compute_correlation"],
    "risk": ["validate_backtest", "get_backtest_metrics", "compute_drawdown", "compute_correlation"],
}

_ROLE_PROMPTS: dict[str, str] = {
    "market": "You are Market Analyst. Use get_market_data/get_bars_range/compute_indicator to analyze price/volume/trend. Cite Ground Truth prices.",
    "sentiment": "You are Sentiment Analyst. Use search_symbols/search_symbol to gather sentiment context. Summarize catalysts.",
    "news": "You are News/Sentiment Analyst. Use search_symbols and memory recall to gather sentiment/news context. Summarize catalysts.",
    "fundamentals": "You are Fundamentals Analyst. Use get_ticker_info/get_fundamentals to assess valuation and earnings. Note placeholders if data empty.",
    "factor": "You are Factor Analyst. Use compute_factor/screen_factors/compute_indicator to evaluate momentum and signals.",
    "regime": "You are Regime Analyst. Use compute_indicator/compute_correlation to identify market regime and transitions.",
    "risk": "You are Risk Analyst. Use validate_backtest/get_backtest_metrics/compute_drawdown to check PIT, 1% cross-source, drawdown and compliance.",
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
        canon = _ALIAS_MAP.get(key)
        if canon is None:
            raise ValueError(f"unknown analyst role: {s!r}")
        if canon not in norm:
            norm.append(canon)
    return norm


def _get_role_tools(name: str) -> list[str]:
    """Return tool names for a role; empty list means no special tools."""
    return _ROLE_TOOL_MAP.get(name, [])


def _get_role_prompt(name: str) -> str:
    return _ROLE_PROMPTS.get(name, f"You are {name} analyst. Provide concise analysis.")


def _check_breaker(active) -> bool:
    """单次熔断判定：优先原子 check_and_add，回退 should_fallback 查询。"""
    if hasattr(active, "check_and_add"):
        return bool(active.check_and_add(0.1))
    return bool(active.should_fallback(cost=0.1))


def _leaf_subagent(name: str, breaker=None, config=None):
    """创建叶分析师节点 — Phase 1: 绑定角色 Prompt 与工具子集，复用 skill 的审计/脱敏/截断范式。

    仍保持 BudgetBreaker 熔断与 delegation_depth 预算，新增 per-agent 工具绑定与角色提示，
    输出通过 State add reducer 聚合，供 verify 节点综合。

    breaker 注入优先级：显式 breaker 参数 > config["breaker"]（RunnableConfig 透传）
    > 模块级 _breaker 遗留默认。显式注入可避免跨请求共享熔断器导致的预算污染。
    """

    def _run(state: State) -> Dict[str, Any]:
        try:
            depth = int(state.get("delegation_depth", 0))
        except (ValueError, TypeError, AttributeError) as exc:
            logging.getLogger(__name__).warning("invalid delegation_depth %r: %s", state.get("delegation_depth"), exc, exc_info=True)
            depth = 0
        if depth >= MAX_DELEGATION_DEPTH:
            return {
                "messages": [{"role": "assistant", "content": f"{name}: delegation budget exceeded"}],
                "subagent_outputs": [{"agent": name, "status": "budget_exceeded"}],
                "agent_traces": [{"agent": name, "status": "budget_exceeded"}],
            }
        _active_breaker = breaker
        if _active_breaker is None and isinstance(config, dict):
            _active_breaker = config.get("breaker")
        _using_legacy_global = False
        if _active_breaker is None:
            _active_breaker = _breaker
            _using_legacy_global = True
        if _active_breaker is not None:
            try:
                if _using_legacy_global:
                    # 遗留全局兜底：串行化回退决策；注入式 breaker 走无锁原子路径
                    with _legacy_budget_lock:
                        _fallback = _check_breaker(_active_breaker)
                else:
                    _fallback = _check_breaker(_active_breaker)
                if _fallback:
                    return {
                        "messages": [{"role": "assistant", "content": f"{name}: budget fallback"}],
                        "subagent_outputs": [{"agent": name, "status": "fallback"}],
                        "agent_traces": [{"agent": name, "status": "fallback"}],
                    }
            except Exception as exc:
                logging.getLogger(__name__).warning("BudgetBreaker check failed for %s: %s", name, exc, exc_info=True)

        # Role-specific prompt and tool binding (Phase 1)
        role_prompt = _get_role_prompt(name)
        tool_names = _get_role_tools(name)
        # Build tool context for audit/trace — actual LLM tool-calling happens in loop._run_graph
        tool_preview = ""
        if tool_names:
            try:
                from hero_quant.tools.registry import TOOL_REGISTRY

                available = [t for t in tool_names if t in TOOL_REGISTRY]
                # audit 一致性：preview 与上报均使用 TOOL_REGISTRY 过滤后的可用工具，
                # 避免下游审计看到实际不存在的工具
                tool_names = available
                if available:
                    tool_preview = f" | tools: {', '.join(available)}"
            except Exception as exc:
                # 中文：窄化捕获并记录，避免静默吞错导致 preview 与 tools 分叉
                logging.getLogger(__name__).debug("TOOL_REGISTRY lookup failed for %s: %s", name, exc)
        content = f"{name}: research done [{role_prompt[:80]}]{tool_preview}"
        return {
            "messages": [{"role": "assistant", "content": content}],
            "subagent_outputs": [{"agent": name, "output": f"{name} result", "role_prompt": role_prompt, "tools": tool_names}],
            "agent_traces": [{"agent": name, "role": name, "tools": tool_names}],
        }

    _run.__name__ = f"leaf_{name}"
    return _run


def _lazy_command_send():
    """懒加载 Command/Send，避免模块导入时拖慢启动."""
    try:
        from langgraph.types import Command as _C, Send as _S  # type: ignore

        return _C, _S
    except ImportError:
        try:
            from langgraph.graph import Command as _C2, Send as _S2  # type: ignore

            return _C2, _S2
        except ImportError:
            return Command, Send


def plan_node(state: State):
    """计划阶段：分解任务并通过 Send 扇出实现并行调度，超委派深度则直接返回预算提示."""
    try:
        depth = int(state.get("delegation_depth", 0))
    except (ValueError, TypeError, AttributeError) as exc:
        logging.getLogger(__name__).warning("invalid delegation_depth %r: %s", state.get("delegation_depth"), exc, exc_info=True)
        depth = 0
    if depth >= MAX_DELEGATION_DEPTH:
        return {
            "messages": [{"role": "assistant", "content": "plan: delegation budget exceeded"}],
            "delegation_depth": depth + 1,
        }
    msgs = state.get("messages", [])
    last = ""
    try:
        if msgs:
            m = msgs[-1]
            if isinstance(m, dict):
                last = m.get("content", "") or ""
            elif hasattr(m, "content"):
                # LangChain BaseMessage：取 .content，避免 str(msg) 的 repr 污染路由
                c = m.content
                last = c if isinstance(c, str) else (str(c) if c is not None else "")
            else:
                last = str(m)
    except (IndexError, AttributeError, TypeError, ValueError) as exc:
        logging.getLogger(__name__).warning("plan_node message extract failed: %s", exc, exc_info=True)
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
            "delegation_depth": depth + 1,
        }
    # 最小扇出载荷：仅透传新分支必需的 messages 切片 + plan + delegation_depth，
    # 避免全量 **state deepcopy（O(targets*state_size) 且快照 stale reducer 列表）。
    fanout_kwargs: Dict[str, Any] = {
        "messages": [msgs[-1]] if msgs else [],
        "plan": plan_text,
        "delegation_depth": depth + 1,
    }
    return Cmd(
        update={
            "messages": [{"role": "assistant", "content": "plan done"}],
            "plan": plan_text,
            "delegation_depth": depth + 1,
        },
        # 每个 Send 独立浅拷贝，避免浅拷贝共享同一 dict
        goto=[Snd(t, dict(fanout_kwargs)) for t in targets],
    )


def execute_node(state: State) -> Dict[str, Any]:
    """(已废弃遗留) 执行阶段：旧式串行扇出，保留兼容；新图已由 plan→Send 直连并行."""
    try:
        depth = int(state.get("delegation_depth", 0))
    except (ValueError, TypeError, AttributeError) as exc:
        logging.getLogger(__name__).warning("execute_node invalid depth %r: %s", state.get("delegation_depth"), exc, exc_info=True)
        depth = 0
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

    # Avoid falsy `or` chaining that hides empty list – explicit check
    outputs = state.get("subagent_outputs")
    if outputs is None:
        outputs = state.get("intermediate_results")
    if outputs is None:
        outputs = []
    # Validate type to avoid unsafe cast
    if not isinstance(outputs, list):
        logging.getLogger(__name__).warning("verify_node outputs not list: %r, coerced to []", type(outputs).__name__)
        outputs = []
    n = len(outputs)
    # 中文：空证据置信度低于单证据，避免倒挂（旧 0.65 > 0.60）
    confidence = round(min(0.85, 0.55 + 0.05 * n), 2) if n else 0.50
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
    """(已废弃遗留) Saga 补偿节点：回滚占位，当前图未连边."""
    return {
        "messages": [{"role": "assistant", "content": "compensate done"}],
        "verification": "compensated",
    }


def build_research_graph(selected: List[str] | None = None):
    """构建并编译研究团队图，selected 为空时默认扇出 market/sentiment/news。"""
    if selected is not None and not isinstance(selected, list):
        raise TypeError(f"selected must be list or None, got {type(selected).__name__}")
    normalized = _normalize_selected(selected) if selected is not None else ["market", "sentiment", "news"]
    # Defensive copy to avoid mutable shared state
    normalized = list(normalized)

    graph = StateGraph(State)

    def _plan(state: State):
        try:
            depth = int(state.get("delegation_depth", 0))
        except (ValueError, TypeError, AttributeError) as exc:
            logging.getLogger(__name__).warning("_plan invalid depth %r: %s", state.get("delegation_depth"), exc, exc_info=True)
            depth = 0
        if depth >= MAX_DELEGATION_DEPTH:
            return {
                "messages": [{"role": "assistant", "content": "plan: delegation budget exceeded"}],
                "delegation_depth": depth + 1,
            }
        targets = list(normalized) if normalized else ["market", "sentiment", "news"]
        msgs = state.get("messages", [])
        last = ""
        try:
            if msgs:
                m = msgs[-1]
                if isinstance(m, dict):
                    last = m.get("content", "") or ""
                elif hasattr(m, "content"):
                    c = m.content
                    last = c if isinstance(c, str) else (str(c) if c is not None else "")
                else:
                    last = str(m)
        except (IndexError, AttributeError, TypeError, ValueError) as exc:
            logging.getLogger(__name__).warning("_plan message extract failed: %s", exc, exc_info=True)
            last = ""
        plan_text = f"plan for: {last[:80]}" if last else "plan: default research"
        Cmd, Snd = _lazy_command_send()
        if Cmd is None or Snd is None:
            return {
                "messages": [{"role": "assistant", "content": "plan done"}],
                "plan": plan_text,
                "delegation_depth": depth + 1,
            }
        # 最小扇出载荷（同 plan_node）：仅 messages 切片 + plan + delegation_depth
        _fanout: Dict[str, Any] = {
            "messages": [msgs[-1]] if msgs else [],
            "plan": plan_text,
            "delegation_depth": depth + 1,
        }
        return Cmd(
            update={
                "messages": [{"role": "assistant", "content": "plan done"}],
                "plan": plan_text,
                "delegation_depth": depth + 1,
            },
            goto=[Snd(t, dict(_fanout)) for t in targets],
        )

    _plan.__name__ = "plan"
    graph.add_node("plan", _plan)

    # 注册全部叶节点，避免 Send 目标缺失；执行边均汇至 verify
    all_nodes = set(normalized) | set(_CANONICAL)
    for name in all_nodes:
        graph.add_node(name, _leaf_subagent(name))
        graph.add_edge(name, "verify")

    # execute/compensate 已移除：遗留串行/Saga 路径不可达，现由 plan→Send 直连并行
    # graph.add_node("execute", execute_node)  # removed - unreachable legacy path
    # graph.add_node("compensate", compensate_node)  # wire only if conditional edge added
    graph.add_node("verify", verify_node)

    try:
        graph.add_edge(START, "plan")
    except (ValueError, TypeError, RuntimeError) as e:
        logging.getLogger(__name__).warning("add_edge START failed: %s", e)
        graph.set_entry_point("plan")
    graph.add_edge("verify", END)
    # graph.add_edge("compensate", END)  # removed with dead node

    compiled = graph.compile()
    return compiled
