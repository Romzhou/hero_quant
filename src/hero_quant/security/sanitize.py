"""输入净化 — ticker 路径组件校验，防目录穿越。

职责：校验外部输入的 ticker 是否可安全拼入文件路径，拦截 ``../`` 等穿越。
安全设计：白名单正则 ``^[A-Za-z0-9._\\-\\^=+]+$`` 且长度 ≤32，拒绝空值、
纯点号与非法字符；允许 ``^GSPC``/``GC=F`` 等合法符号，阻断路径逃逸。
"""

from __future__ import annotations

import re

# 允许字符：字母/数字/点/中划线/下划线/尖号(^GSPC)/等号(GC=F)/加号(XAUUSD+)
# 以上均不具备目录穿越能力，超出此集合的字符一律拒绝
_TICKER_PATH_RE = re.compile(r"^[A-Za-z0-9._\-\^=+]+$")


def safe_ticker_component(ticker: str, *, max_len: int = 32) -> str:
    """校验 ticker 是否可安全拼入文件路径；合法则原样返回，否则抛 ValueError。"""
    if not isinstance(ticker, str) or not ticker:
        raise ValueError(f"ticker must be a non-empty string, got {ticker!r}")
    if len(ticker) > max_len:
        raise ValueError(f"ticker exceeds {max_len} chars: {ticker!r}")
    if not _TICKER_PATH_RE.fullmatch(ticker):
        raise ValueError(
            f"ticker contains characters not allowed in a filesystem path: {ticker!r}"
        )
    # 正则允许 '.'，需额外拒绝纯点号（如 '.'/'..'），否则仍可穿越父目录
    if set(ticker) == {"."}:
        raise ValueError(f"ticker cannot consist solely of dots: {ticker!r}")
    return ticker
