"""Neutralize model boundary tokens and remove zero-width/invisible characters."""

from __future__ import annotations

import re
import unicodedata

# Cover the delimiter forms used by ChatML, Qwen, DeepSeek, Llama, and Gemma.
# - Increased upper bound to 200 to avoid bypass via >80 chars; no hard upper bound would be safer but 200 balances false positives.
# - Fullwidth variant now excludes both ASCII '>'/'|' and fullwidth '＞'/'｜'.
# - </?s> is word-bounded to avoid flagging "a <s> b" benign usage.
_SPECIAL_TOKEN_RE = re.compile(
    r"(?:"
    r"<\|[^>\r\n＞｜\|]{1,200}\|>"
    r"|<｜[^>\r\n＞\u007c\uFF5c]{1,200}｜>"
    r"|(?<!\w)</?s>(?!\w)"
    r"|\[/?(?:INST|SYS|USER|ASSISTANT)\]"
    r"|<<SYS>>"
    r"|<</SYS>>"
    r"|<(?:bos|eos|start_of_turn|end_of_turn|start_of_image|end_of_image)>"
    r")",
    re.IGNORECASE,
)

# Extended invisible/Cf coverage: zero-width + bidi + soft hyphen etc.
# Includes U+200B/C/D, FEFF, 200E/F (LRM/RLM), 202A-202E (bidi), 2060, 2066-206F, 180E, 00AD, 034F, 061C, 115F/1160/3164/FFA0
_INVISIBLE_CHARS = (
    "\u200b\u200c\u200d\ufeff"
    "\u200e\u200f"
    "\u202a\u202b\u202c\u202d\u202e"
    "\u2060\u2066\u2067\u2068\u2069\u206a\u206b\u206c\u206d\u206e\u206f"
    "\u180e\u00ad\u034f\u061c"
    "\u115f\u1160\u3164\uffa0"
)
_ZERO_WIDTH_TRANSLATION = str.maketrans("", "", _INVISIBLE_CHARS)


def _escape_special_token(match: re.Match[str]) -> str:
    """Escape token delimiters atomically, preventing reconstruction via suffix match.

    Replaces all delimiters in the matched token: < > [ ] | and fullwidth variants.
    Uses 6-char literal \\uXXXX to avoid re-decode to actual delimiter downstream.
    """
    s = match.group(0)
    # Escape in order that avoids double-escaping the backslashes we introduce
    s = s.replace("<", r"\u003c")
    s = s.replace(">", r"\u003e")
    s = s.replace("[", r"\u005b")
    s = s.replace("]", r"\u005d")
    s = s.replace("|", r"\u007c")
    s = s.replace("｜", r"\u007c")
    s = s.replace("＞", r"\u003e")
    return s


def neutralize(text: str) -> str:
    """Escape recognized model boundary tokens in ``text`` idempotently.

    Canonicalization order: strip invisible/bidi characters and NFKC-normalize first,
    then match tokens. This prevents evasion via zero-width inserted inside delimiters
    e.g. ``<\\u200b|im_start|>``. Idempotency is preserved because escaped form
    no longer matches the regex.
    """
    # Normalize invisibles and fullwidth variants before matching
    # Strip extended invisible set so embedded zero-width inside token is removed
    cleaned = text.translate(_ZERO_WIDTH_TRANSLATION)
    # NFKC handles fullwidth homoglyph variants (e.g., fullwidth ＜)
    try:
        cleaned = unicodedata.normalize("NFKC", cleaned)
    except Exception:
        pass
    return _SPECIAL_TOKEN_RE.sub(_escape_special_token, cleaned)


def strip_zero_width(text: str) -> str:
    """Remove the zero-width / invisible characters commonly used to evade scanners."""
    return text.translate(_ZERO_WIDTH_TRANSLATION)


def sanitize(text: str) -> str:
    """Atomic helper: strip invisibles -> NFKC -> neutralize in one step."""
    # Order matters: strip then NFKC then neutralize
    cleaned = strip_zero_width(text)
    try:
        cleaned = unicodedata.normalize("NFKC", cleaned)
    except Exception:
        pass
    return _SPECIAL_TOKEN_RE.sub(_escape_special_token, cleaned)
