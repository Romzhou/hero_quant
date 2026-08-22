"""Neutralize model boundary tokens and remove zero-width characters."""

from __future__ import annotations

import re


# Cover the delimiter forms used by ChatML, Qwen, DeepSeek, Llama, and Gemma.
# The two pipe-delimited forms intentionally accept future role/control names.
_SPECIAL_TOKEN_RE = re.compile(
    r"(?:"
    r"<\|[^>\r\n]{1,80}\|>"
    r"|<｜[^>\r\n]{1,80}｜>"
    r"|</?s>"
    r"|\[/?(?:INST|SYS|USER|ASSISTANT)\]"
    r"|<<SYS>>"
    r"|<</SYS>>"
    r"|<(?:bos|eos|start_of_turn|end_of_turn|start_of_image|end_of_image)>"
    r")",
    re.IGNORECASE,
)

_ZERO_WIDTH_TRANSLATION = str.maketrans("", "", "\u200b\u200c\u200d\ufeff")


def _escape_special_token(match: re.Match[str]) -> str:
    """Escape token delimiters without changing the rest of the token text."""
    return match.group(0).replace("<", r"\u003c").replace("[", r"\u005b")


def neutralize(text: str) -> str:
    """Escape recognized model boundary tokens in ``text`` idempotently."""
    return _SPECIAL_TOKEN_RE.sub(_escape_special_token, text)


def strip_zero_width(text: str) -> str:
    """Remove the zero-width characters commonly used to evade scanners."""
    return text.translate(_ZERO_WIDTH_TRANSLATION)
