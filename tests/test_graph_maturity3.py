"""Wave B4-1: Send fanout TDD — plan -> [Send]*3 -> verify with Annotated[list,add] reducer."""

from __future__ import annotations

import inspect
import re


def test_pros_cons():
    """B4-2: verify 应轻量 pros/cons 对抗 — pros/cons/confidence 一次生成，非3轮风险链."""
    from hero_quant.agent.graph import build_research_graph, verify_node

    src = inspect.getsource(verify_node)
    assert "请给出多空两面 pros/cons + 置信度" in src or (
        "pros/cons" in src and "置信度" in src
    ), f"verify prompt 应含 pros/cons + 置信度, got {src[:800]!r}"
    assert "confidence" in src.lower() or "置信度" in src

    try:
        graph = build_research_graph(selected=["market", "sentiment", "news"])
    except TypeError:
        graph = build_research_graph()  # type: ignore[call-arg]
    out = graph.invoke({"messages": [{"role": "user", "content": "test pros cons verify"}]})
    ver = out.get("verification", "")
    blob = str(ver)
    pros = out.get("pros")
    cons = out.get("cons")
    conf = out.get("confidence")
    if pros is not None and cons is not None and conf is not None:
        assert isinstance(pros, list) and len(pros) >= 1, f"pros should be non-empty list, got {pros!r}"
        assert isinstance(cons, list) and len(cons) >= 1, f"cons should be non-empty list, got {cons!r}"
        assert 0 <= float(conf) <= 1, f"confidence should be 0-1, got {conf!r}"
        blob += str(pros) + str(cons) + str(conf)
    low = blob.lower()
    assert "pros" in low, f"verify 输出应含 pros, got {blob!r}"
    assert "cons" in low, f"verify 输出应含 cons, got {blob!r}"
    assert "confidence" in low, f"verify 输出应含 confidence, got {blob!r}"
    m = re.search(r"confidence[:\s]*0\.\d+", low)
    assert m is not None, f"confidence 应为 0.x 格式, got {blob!r}"


def test_fanout():
    """B4-1 red->green: plan 含 market/sentiment/news 时应 Command goto 3 个 Send 且 verify 节点 Annotated[list,add] 归约."""
    from hero_quant.agent.graph import plan_node, build_research_graph
    from hero_quant.agent.state import State
    from langgraph.types import Command, Send

    # 1) plan_node 应返回 Command(goto=[Send(...)]*3)
    state: State = {
        "messages": [{"role": "user", "content": "research market sentiment news"}],
        "plan": "market sentiment news",
        "delegation_depth": 0,
    }  # type: ignore[typeddict-item]
    result = plan_node(state)
    assert isinstance(result, Command), f"plan_node should return Command, got {type(result).__name__}: {result!r}"
    # Command.goto 应包含 3 个 Send
    goto = result.goto
    # Normalize to list
    sends = list(goto) if isinstance(goto, (list, tuple)) else [goto]
    send_nodes = [s for s in sends if isinstance(s, Send)]
    assert len(send_nodes) == 3, f"expected 3 Sends for market/sentiment/news, got {len(send_nodes)}: {sends!r}"
    node_names = {s.node for s in send_nodes}
    assert node_names == {"market", "sentiment", "news"}, f"Send nodes mismatch: {node_names}"

    # 2) verify 节点 Annotated[list,add] 归约 — State 上 subagent_outputs / intermediate_results 必须是 Annotated[list, add]
    from typing import get_origin, get_args

    ann_sub = State.__annotations__.get("subagent_outputs")
    ann_inter = State.__annotations__.get("intermediate_results")
    assert ann_sub is not None, "State.subagent_outputs missing"
    assert ann_inter is not None, "State.intermediate_results missing"
    # Check is Annotated
    for ann in (ann_sub, ann_inter):
        s = str(ann)
        # Annotated present or get_origin is Annotated
        origin = get_origin(ann)
        # typing.Annotated has no stable origin check across py versions, fallback to string
        assert "Annotated" in s or origin is not None, f"should be Annotated[list, add], got {ann!r}"
    # Ensure reducer is _add_list style — check that annotation metadata contains callable that behaves as add
    # At minimum, string contains "Annotated"
    assert "Annotated" in str(ann_sub), f"subagent_outputs should be Annotated[list, add], got {ann_sub!r}"

    # 3) 集成：图调用后并行归约 — invoke 应聚合 3 路输出
    # 兼容两种签名：build_research_graph() 或 build_research_graph(selected=[...])
    try:
        graph = build_research_graph(selected=["market", "sentiment", "news"])
    except TypeError:
        graph = build_research_graph()
    out = graph.invoke({"messages": [{"role": "user", "content": "test market sentiment news"}]})
    outputs = out.get("subagent_outputs") or out.get("intermediate_results") or []
    # 来自 3 个并行 leaf 的输出应归约在一起
    assert len(outputs) >= 3, f"expected >=3 parallel subagent_outputs via add reducer, got {len(outputs)}: {outputs!r}"
    agents = {o.get("agent") for o in outputs if isinstance(o, dict)}
    # 至少包含市场/情绪/新闻三者之一集合
    assert {"market", "sentiment", "news"} <= agents or len(agents) >= 3, f"agents missing: {agents}"

    # 4) delegationDepth 保持 5 预算 — depth>=5 时应 budget 截断
    state_over: State = {
        "messages": [{"role": "user", "content": "x"}],
        "delegation_depth": 5,
    }  # type: ignore[typeddict-item]
    result_over = plan_node(state_over)
    # 超预算时不 fanout，返回消息提示 budget
    if isinstance(result_over, Command):
        # 若仍为 Command，其 update 至少含预算提示或不再 fanout？宽松：允许返回 Command 但不应再含 3 Sends
        sends_over = [s for s in (list(result_over.goto) if isinstance(result_over.goto, (list, tuple)) else [result_over.goto]) if isinstance(s, Send)]
        assert len(sends_over) == 0, f"delegationDepth 5 应截断 fanout, got {sends_over}"
    else:
        # dict 情况检查包含 budget 关键字
        txt = str(result_over)
        assert "budget" in txt.lower() or "exhaust" in txt.lower() or "exceeded" in txt.lower()
