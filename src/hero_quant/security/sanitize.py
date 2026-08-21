"""Sanitize — safe_ticker_component ported from TradingAgents dataflows/utils.py.

Source: TradingAgents/tradingagents/dataflows/utils.py:9-42
Regex: ^[A-Za-z0-9._\\-\\^=+]+$ , len<=32, reject empty / pure dots / traversal.
No extra dependencies.
"""

from __future__ import annotations

import re

# Tickers can contain letters, digits, dot, dash, underscore, caret
# (index symbols like ^GSPC), equals (futures like GC=F), and plus
# (forex/CFD symbols like XAUUSD+). None of these enable directory
# traversal, so the value never escapes a containing directory when
# interpolated into a path. Anything else is rejected.
_TICKER_PATH_RE = re.compile(r"^[A-Za-z0-9._\-\^=+]+$")


def safe_ticker_component(ticker: str, *, max_len: int = 32) -> str:
    """Validate ``ticker`` is safe to interpolate into a filesystem path.

    Tickers come from user CLI input or from LLM tool calls, both of which
    can be influenced by attacker-controlled content (e.g. prompt injection
    embedded in fetched news). Without validation, a value like
    ``"../../../etc/foo"`` flows into ``os.path.join`` / ``Path /`` and
    escapes the configured cache, checkpoint, or results directory.

    Returns ``ticker`` unchanged when it matches the allowed pattern; raises
    ``ValueError`` otherwise.
    """
    if not isinstance(ticker, str) or not ticker:
        raise ValueError(f"ticker must be a non-empty string, got {ticker!r}")
    if len(ticker) > max_len:
        raise ValueError(f"ticker exceeds {max_len} chars: {ticker!r}")
    if not _TICKER_PATH_RE.fullmatch(ticker):
        raise ValueError(
            f"ticker contains characters not allowed in a filesystem path: {ticker!r}"
        )
    # The regex above allows '.', so values like '.', '..', '...' would pass,
    # and as a path component they traverse the parent directory. Reject any
    # value that's only dots.
    if set(ticker) == {"."}:
        raise ValueError(f"ticker cannot consist solely of dots: {ticker!r}")
    return ticker
