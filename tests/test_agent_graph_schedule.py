"""Task17 TDD: graph 调度深度（scan_core_muse.log:495-549）——运行时断言取代源码文本扫描。"""

from hero_quant.agent.graph import MAX_DELEGATION_DEPTH, plan_node


def _state(depth: int) -> dict:
    return {
        "messages": [{"role": "user", "content": "research market sentiment news"}],
        "plan": "market sentiment news",
        "delegation_depth": depth,
    }


def test_delegation_depth_increments():
    """plan_node 的 update 与每个 Send 载荷都必须把 delegation_depth 加一。"""
    for depth in (0, 2):
        res = plan_node(_state(depth))
        assert res.update["delegation_depth"] == depth + 1
        sends = [s for s in res.goto if getattr(s, "arg", None) is not None]
        assert sends, "plan_node 应通过 Send 扇出"
        for send in sends:
            assert send.arg["delegation_depth"] == depth + 1


def test_delegation_depth_budget_exceeded():
    """达到上限时不再扇出，直接返回预算提示且深度仍加一。"""
    res = plan_node(_state(MAX_DELEGATION_DEPTH))
    assert isinstance(res, dict)
    assert res["delegation_depth"] == MAX_DELEGATION_DEPTH + 1
    assert "budget exceeded" in res["messages"][0]["content"]
