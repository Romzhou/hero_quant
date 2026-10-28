"""Round7 X2a — `ContextManager._collapse` 预算契约锁测试。

背景：`src/hero_quant/agent/context.py` 零预算分支曾写着
"Avoid division by zero / empty output: return marker truncated"，与
`marker[:0]` 的实际行为（空串）相反。该行为本身是**设计如此**：
预算 0 ⇒ 输出 0 字符，符合「返回值长度不超过 max_chars」的预算契约
（返回 marker 会突破预算）。Round7 只订正了注释措辞，行为零改动。

本文件只断言**外部可观察行为**（返回值与其长度），不触碰内部变量，
用于锁死边界：零预算、单字符预算、小预算、预算内原样、长文本折叠。
"""

from hero_quant.agent.context import ContextManager


def test_collapse_zero_budget_returns_empty_string():
    """零预算 ⇒ 空串（预算契约的直接结果，不允许返回 marker 突破 0 预算）。"""
    assert ContextManager._collapse("abcdef", 0) == ""


def test_collapse_budget_one_truncates_to_exactly_one_char():
    """预算 1：marker 被截断到恰好 1 个字符，长度不得超预算。"""
    out = ContextManager._collapse("abcdef", 1)
    assert len(out) == 1


def test_collapse_budget_five_fits_within_budget():
    """预算 5：短文本折叠后长度不超过 5（小预算下窗口收缩 + marker 截断）。"""
    out = ContextManager._collapse("abcdef", 5)
    assert len(out) <= 5


def test_collapse_within_budget_returns_text_unchanged():
    """预算内原样返回，不插 marker。"""
    assert ContextManager._collapse("abc", 100) == "abc"


def test_collapse_long_text_budget_1000_keeps_marker_and_budget():
    """长文本 + 预算 1000：含折叠 marker 且总长不超过 1000。"""
    text = "A" * 1200 + "B" * 1200
    out = ContextManager._collapse(text, 1000)
    assert "[COLLAPSED head=" in out
    assert len(out) <= 1000
