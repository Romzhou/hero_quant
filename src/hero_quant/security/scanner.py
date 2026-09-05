"""中和模型边界 token 并处理零宽/不可见字符。

设计：检测与输出分离——在“检测副本”（类别驱动剥离 + 逐字符 NFKC）上匹配，
再把命中 span 映射回原文做转义后拼接。无命中时原文逐字保留（不整体 NFKC 改写，
不删除 ZWJ/ZWNJ/软连字符等多语义字符）；strip_zero_width 仍保留全量剥离语义，
供脱敏管线彻底清除零宽。
"""

from __future__ import annotations

import re
import logging
import unicodedata

# 中文：覆盖 ChatML、Qwen、DeepSeek、Llama、Gemma 的分隔符形态。
# - 上限 200 防超长旁路；过长误报与漏报的折中。
# - `</?s>` 不做单词边界隔离：`(?<!\w)/(?!\w)` 曾让 `a<s>b` 旁路（tokenizer 仍可识别
#   紧贴文本的 token）。改为无条件匹配 `</s>`；`<s` 后加 `(?![A-Za-z])` 前瞻，仅防
#   `<strong>` 类嵌入单词误伤（字面 `>` 本就阻止 `<s` 前缀误配 `<strong>` 以外场景）。
# - 全宽变体不单独设分支：检测副本已 NFKC，`｜/＞/＜` 折叠为 ASCII 后走统一分支。
_SPECIAL_TOKEN_RE = re.compile(
    r"(?:"
    r"<\|[^>\r\n\|]{1,200}\|>"
    r"|</s>|<s(?![A-Za-z])"
    r"|\[/?(?:INST|SYS|USER|ASSISTANT)\]"
    r"|<<SYS>>"
    r"|<</SYS>>"
    r"|<(?:bos|eos|start_of_turn|end_of_turn|start_of_image|end_of_image)>"
    r"|\\u003c\|[^|\r\n\\]{1,200}\|\\u003e"
    r")",
    re.IGNORECASE,
)

# Extended invisible/Cf coverage: zero-width + bidi + soft hyphen etc.
# strip_zero_width 沿用全量剥离（含 ZWJ/ZWNJ/soft hyphen/Hangul filler），脱敏管线依赖其彻底性。
_INVISIBLE_CHARS = (
    "\u200b\u200c\u200d\ufeff"
    "\u200e\u200f"
    "\u202a\u202b\u202c\u202d\u202e"
    "\u2060\u2066\u2067\u2068\u2069\u206a\u206b\u206c\u206d\u206e\u206f"
    "\u180e\u00ad\u034f\u061c"
    "\u115f\u1160\u3164\uffa0"
)
_ZERO_WIDTH_TRANSLATION = str.maketrans("", "", _INVISIBLE_CHARS)

# 中文：neutralize 输出级最小剥离——只去无连接/断行语义的纯零宽与格式控制，
# 保留 ZWJ/ZWNJ（emoji 序列与印欧语连接）、软连字符（断行）、Hangul 填充，
# 避免破坏合法多语言文本。
_OUTPUT_STRIP_CHARS = (
    "\u200b\ufeff"
    "\u200e\u200f"
    "\u202a\u202b\u202c\u202d\u202e"
    "\u2060\u2066\u2067\u2068\u2069\u206a\u206b\u206c\u206d\u206e\u206f"
    "\u180e\u034f\u061c"
)
_OUTPUT_STRIP_TABLE = str.maketrans("", "", _OUTPUT_STRIP_CHARS)

# 中文：检测用剥离类别——Cf（含 variation selector 等格式控制）+ Mn（变体选择符等
# 非间距标记）+ Zl/Zp（行段分隔）；检测副本剥离全部（含 ZWJ/ZWNJ），因为切分 token
# 的连接符对下游 tokenizer 同样透明（`<\u200D|im_start|>` 仍可被识别为边界 token）；
# 非命中文本原样拼回，emoji ZWJ 序列等合法内容不受影响（输出级仍保留 ZWJ/ZWNJ）。
_STRIP_CATEGORIES = frozenset({"Cf", "Mn", "Zl", "Zp"})
_DETECTION_PRESERVE: frozenset[str] = frozenset()


def _detection_copy(text: str) -> tuple[str, list[int], list[int]]:
    """构建检测副本并返回 (norm, norm2kept, kept2orig) 索引映射。

    中文：逐字符 NFKC 并记录 norm 偏移→原文偏移，保证命中 span 可映射回原文；
    异常窄化捕获（仅文本/值错误），避免吞没无关异常。
    """
    kept_chars: list[str] = []
    kept2orig: list[int] = []
    for j, ch in enumerate(text):
        if ch not in _DETECTION_PRESERVE and unicodedata.category(ch) in _STRIP_CATEGORIES:
            continue
        kept_chars.append(ch)
        kept2orig.append(j)
    norm_parts: list[str] = []
    norm2kept: list[int] = []
    try:
        for i, ch in enumerate(kept_chars):
            folded = unicodedata.normalize("NFKC", ch)
            norm_parts.append(folded)
            norm2kept.extend([i] * len(folded))
    except (ValueError, TypeError, AttributeError) as e:
        logging.getLogger(__name__).debug("scanner.normalize_failed: %s", e)
        norm_parts = list(kept_chars)
        norm2kept = list(range(len(kept_chars)))
    return "".join(norm_parts), norm2kept, kept2orig


def _escape_span(norm_token: str, orig_span: str) -> str:
    """转义映射回原文的命中 span。

    中文：先滤掉检测期剥离字符（Cf/Mn/Zl/Zp，如 U+2064、variation selector），
    避免其经原文 span 重新进入输出；再原子转义全部定界符（含全宽 `｜/＞/＜`，
    其经 NFKC 折叠后命中、原文 span 中仍为全宽形态——含预转义分支统一走
    _escape_text，与存量 `\\uXXXX` 转义同 depth（下游单次解码不直接还原 token）。
    """
    filtered = "".join(
        ch for ch in orig_span if unicodedata.category(ch) not in _STRIP_CATEGORIES
    )
    return _escape_text(filtered if filtered else orig_span)


def _escape_text(s: str) -> str:
    """Escape token delimiters atomically, preventing reconstruction via suffix match.

    Replaces all delimiters in the matched span: < > [ ] | and fullwidth variants.
    Uses 6-char literal \\uXXXX to avoid re-decode to actual delimiter downstream.
    """
    # Escape in order that avoids double-escaping the backslashes we introduce
    s = s.replace("<", r"\u003c")
    s = s.replace(">", r"\u003e")
    s = s.replace("[", r"\u005b")
    s = s.replace("]", r"\u005d")
    s = s.replace("|", r"\u007c")
    s = s.replace("｜", r"\u007c")
    s = s.replace("＞", r"\u003e")
    s = s.replace("＜", r"\u003c")
    return s


def neutralize(text: str) -> str:
    """Escape recognized model boundary tokens in ``text`` idempotently.

    Canonicalization order: strip output-safe invisibles, then match on a
    detection copy (category-stripped + per-char NFKC) and splice escapes back
    into the original offsets. This prevents evasion via zero-width inserted
    inside delimiters e.g. ``<\\u200b|im_start|>`` while leaving non-token
    multilingual text (emoji ZWJ sequences, ﬁ/①/fullwidth) untouched.
    Idempotency is preserved because escaped form no longer matches the regex.
    """
    # 输出级最小剥离：只去纯零宽/格式控制，保留 ZWJ/ZWNJ/软连字符等多语义字符
    text = text.translate(_OUTPUT_STRIP_TABLE)
    norm, norm2kept, kept2orig = _detection_copy(text)
    if not norm:
        return text
    parts: list[str] = []
    orig_cursor = 0
    for m in _SPECIAL_TOKEN_RE.finditer(norm):
        ks = norm2kept[m.start()]
        ke = norm2kept[m.end() - 1] + 1
        o_start = kept2orig[ks]
        o_end = kept2orig[ke - 1] + 1
        parts.append(text[orig_cursor:o_start])
        parts.append(_escape_span(m.group(0), text[o_start:o_end]))
        orig_cursor = o_end
    parts.append(text[orig_cursor:])
    return "".join(parts)


def strip_zero_width(text: str) -> str:
    """Remove the zero-width / invisible characters commonly used to evade scanners."""
    return text.translate(_ZERO_WIDTH_TRANSLATION)


def sanitize(text: str) -> str:
    """Alias of neutralize(): 输出级最小剥离 + 检测副本匹配 + 原文定位转义。

    中文：委托 neutralize 统一实现，避免与 neutralize 重复实现分叉；
    与 neutralize 对同输入结果一致。彻底清除零宽请用 strip_zero_width。
    """
    # 中文：复用 neutralize 统一转义逻辑，避免与 neutralize 重复实现分叉
    return neutralize(text)
