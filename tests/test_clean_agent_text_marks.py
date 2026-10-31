"""`_clean_agent_text` 对 PR-152 不可信边界块的剥离（缺陷 1 回归）。

背景：`security/untrusted.py:wrap_tool_output` 把工具结果包成
`<<UNTRUSTED_TOOL_DATA tool="X">>` / 固定提示行 / `[tool X result] …` / `<<END_UNTRUSTED_TOOL_DATA>>`
后回喂模型，模型可能整段复述到最终回答。旧清洗只按行过滤 `[tool …` 前缀，
边界行与固定提示行会漏进用户可见答案（上一批评测暴露）。

契约（本文件锁死）：
- 完整块（含中间内容与固定提示行）整块剥离；
- 未闭合块从开标记剥到文本末尾；
- 只认这两个精确 tag（大小写敏感），普通正文里的 `a << b` 不受影响；
- 既有 `[tool …]` / `tool_error:` 行过滤与「清洗后为空回退原文」语义不回归；
- 两种空结果分治：剥过不可信块 → 空答（绝不回退原文泄露工具内部数据）；
  没剥过块（如只有 `[ERROR: …]`）→ 保持原有回退原文语义。
"""

from __future__ import annotations

from hero_quant.api.server import _clean_agent_text
from hero_quant.security.untrusted import (
    UNTRUSTED_CLOSE_TAG,
    UNTRUSTED_NOTICE,
    UNTRUSTED_OPEN_TAG,
    wrap_tool_output,
)

OPEN = f"<<{UNTRUSTED_OPEN_TAG}"
CLOSE = f"<<{UNTRUSTED_CLOSE_TAG}>>"


def test_complete_block_is_stripped_entirely():
    """完整块（开标记/固定提示/原始 JSON/闭标记）不得出现在清洗结果里。"""
    block = wrap_tool_output("run_backtest", '{"ok": true, "equity": [1.0, 1.01]}')
    text = "先给结论：近一年收益约 1%。\n" + block + "\n补充风险提示。"
    out = _clean_agent_text(text)

    assert "UNTRUSTED_TOOL_DATA" not in out
    assert UNTRUSTED_NOTICE not in out
    assert "equity" not in out
    # 模型自己的叙述保留
    assert "先给结论：近一年收益约 1%。" in out
    assert "补充风险提示。" in out


def test_complete_block_handwritten_form_also_stripped():
    """手写同形块（模型复述）同样整块剥离，不依赖 wrapper 具体行数。"""
    text = (
        f'{OPEN} tool="get_market_data" flagged=ignore_instructions>>\n'
        f"{UNTRUSTED_NOTICE}\n"
        '[tool get_market_data result] {"bars": [{"close": 1680.2}]}\n'
        f"{CLOSE}\n"
        "结论：年化波动率约 18%。"
    )
    out = _clean_agent_text(text)
    assert "UNTRUSTED_TOOL_DATA" not in out
    assert "1680.2" not in out
    assert out == "结论：年化波动率约 18%。"


def test_unclosed_block_stripped_to_end():
    """未闭合块（闭标记缺失）从开标记剥到文本末尾，不留半段原文。"""
    text = (
        "开头叙述。\n"
        f'{OPEN} tool="get_market_data">>\n'
        f"{UNTRUSTED_NOTICE}\n"
        '[tool get_market_data result] {"bars": [{"close": 1680.2}]}'
    )
    out = _clean_agent_text(text)
    assert out == "开头叙述。"
    assert "UNTRUSTED_TOOL_DATA" not in out
    assert "1680.2" not in out


def test_multiple_blocks_all_stripped():
    """多块污染：逐块剥离，块间叙述保留。"""
    blk = wrap_tool_output("tool_a", '{"x": 1}')
    text = f"甲。\n{blk}\n乙。\n{blk}\n丙。"
    out = _clean_agent_text(text)
    assert "UNTRUSTED_TOOL_DATA" not in out
    assert "甲。" in out and "乙。" in out and "丙。" in out


def test_ordinary_shift_operators_untouched():
    """普通 `<<` 正文（比较/位移/箭头）不受影响——只认两个精确 tag。"""
    text = "残差满足 a << b，位移运算 1 << 3 = 8，箭头 <<< 也不是边界。"
    assert _clean_agent_text(text) == text


def test_tag_matching_is_case_sensitive():
    """大小写变体不是真 tag：不得整块吞掉（否则会误删用户自己的内容）。"""
    text = "<<untrusted_tool_data tool=\"x\">>\n保留这一行\n<<END_UNTRUSTED_TOOL_DATA>>"
    out = _clean_agent_text(text)
    assert "保留这一行" in out


def test_existing_tool_line_filters_not_regressed():
    """既有过滤规则不动：`[tool …] {…}`、`tool_error:` 行仍被丢弃。"""
    text = "\n".join(
        [
            "正常叙述。",
            '[tool run_backtest result] {"ok": true}',
            "[tool run_backtest",
            "tool_error: boom",
            "tool_not_found: nope",
            "[ERROR: timeout]",
            "结尾叙述。",
        ]
    )
    out = _clean_agent_text(text)
    assert out == "正常叙述。\n结尾叙述。"


def test_only_untrusted_block_returns_empty_answer():
    """buffer 只有工具块、没有模型叙述：必须返回空答，绝不回退原文泄露工具内部数据。"""
    block = wrap_tool_output(
        "get_market_data", '{"bars": [{"date": "2024-11-01", "close": 1680.2}]}'
    )
    out = _clean_agent_text(block)
    assert out == ""
    assert "UNTRUSTED_TOOL_DATA" not in out
    assert UNTRUSTED_NOTICE not in out
    assert "1680.2" not in out


def test_only_unclosed_untrusted_block_returns_empty_answer():
    """未闭合且无叙述同样返回空答（剥完为空不得回退）。"""
    text = f'{OPEN} tool="x">>\n{UNTRUSTED_NOTICE}\n[tool x result] {{"close": 1.0}}'
    out = _clean_agent_text(text)
    assert out == ""
    assert "UNTRUSTED_TOOL_DATA" not in out
    assert "close" not in out


def test_no_block_all_noise_keeps_legacy_text_fallback():
    """无不可信块但全是旧噪声行：保持既有「清洗后为空回退原文」语义（不回归）。"""
    noise = "\n".join(
        [
            '[tool run_backtest result] {"ok": true}',
            "tool_error: boom",
            "[ERROR: timeout]",
            "[TRUNCATED: 2000]",
        ]
    )
    assert _clean_agent_text(noise) == noise


def test_empty_input_and_empty_result_fallbacks():
    """空输入原样返回；无不可信块时清洗后为空按既有语义回退原文。"""
    assert _clean_agent_text("") == ""
    noisy = '[tool run_backtest result] {"ok": true}\ntool_error: boom'
    assert _clean_agent_text(noisy) == noisy
