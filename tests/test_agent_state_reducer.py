"""Task19 state reducer tests - TDD Step1."""

from hero_quant.agent.state import _add_messages, _max_depth


def test_add_messages_empty_dict_not_dropped():
    assert _add_messages({}, [{"role": "user", "content": "hi"}]) == [{}, {"role": "user", "content": "hi"}]


def test_delegation_depth_max_reducer():
    assert _max_depth(None, 3) == 3
    assert _max_depth(2, 5) == 5
    assert _max_depth(4, None) == 4
