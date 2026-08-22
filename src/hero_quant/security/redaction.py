"""敏感信息脱敏 — 按 sink 分级的瀑布式脱敏。

职责：对落盘与回传载荷中的密钥、令牌做脱敏，防止日志/追踪中泄露。
安全设计：ARGUMENTS_SINK 最严格（参数全量脱敏），RESULT_SINK 宽松（允许
content 透传但仍拦截顶层密钥）；覆盖 Bearer/sk-/AKIA/JWT 等常见密钥形态。
"""

from __future__ import annotations

import re
from typing import Any

from . import scanner

ARGUMENTS_SINK = "arguments"
RESULT_SINK = "result"

# 敏感键精确匹配（统一小写比较）
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "api-key",
    "secret",
    "password",
    "passwd",
    "token",
    "access_token",
    "refresh_token",
    "client_secret",
    "private_key",
}

# 键名子串匹配——覆盖命名变体（如 my_secret_key）
_SENSITIVE_SUBSTRINGS = ("api_key", "apikey", "secret", "password", "token")

# 密钥值模式——按 sink 区分严格程度
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-_\.=~\+/]+=*", re.IGNORECASE)  # HTTP Bearer 头
_SK_RE = re.compile(r"sk-[A-Za-z0-9]{10,}")  # OpenAI 风格 sk- 密钥
_AKIA_RE = re.compile(r"AKIA[0-9A-Z]{16}")  # AWS Access Key
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")  # JWT 三段式
# 通用长 token（备用，未在当前瀑布中启用）
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{32,}")

_REDACTED = "***"


# LLM 计量键不应脱敏，避免影响 VCR 回放与成本统计
_ALLOW_TOKENS = {"input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "prompttokens", "completiontokens", "generated_tokens"}


def _is_sensitive_key(key: str) -> bool:
    """判断键名是否敏感；计量类 token 键显式放行，避免误杀。"""
    lk = key.lower()
    if lk in _ALLOW_TOKENS:
        return False
    if lk in _SENSITIVE_KEYS:
        return True
    for sub in _SENSITIVE_SUBSTRINGS:
        if sub in lk:
            return True  # 命中子串即视为敏感（如 access_token）
    return False


def _redact_string(value: str, sink: str) -> str:
    """按 sink 对字符串值做模式脱敏；命中任一密钥形态即替换为 ***。"""
    if sink == ARGUMENTS_SINK:
        # 参数槽最严格——任意密钥形态均脱敏
        if _BEARER_RE.search(value):
            return _REDACTED
        if _SK_RE.search(value):
            return _REDACTED
        if _AKIA_RE.search(value):
            return _REDACTED
        if _JWT_RE.search(value):
            return _REDACTED
        return value
    if sink == RESULT_SINK:
        # 结果槽仍需拦截顶层密钥，content 字段的放行由上层处理
        if _BEARER_RE.search(value) or _SK_RE.search(value) or _AKIA_RE.search(value) or _JWT_RE.search(value):
            return _REDACTED
        return value
    return value


def _scan_string(value: str, *, preserve_zero_width: bool = False) -> str:
    """Apply scanner steps without allowing scanner failures to break redaction."""
    try:
        scanned = scanner.neutralize(value)
        if not preserve_zero_width:
            scanned = scanner.strip_zero_width(scanned)
        return scanned
    except Exception:
        return value


def _neutralize_content(value: Any) -> Any:
    """Neutralize result content while preserving secret-like text and zero-width chars."""
    if isinstance(value, str):
        return _scan_string(value, preserve_zero_width=True)
    if isinstance(value, dict):
        return {key: _neutralize_content(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_neutralize_content(item) for item in value]
    return value


def redact_payload(payload: Any, sink: str = ARGUMENTS_SINK) -> Any:
    """按 sink 瀑布对载荷脱敏：敏感键一律替换，字符串值按模式匹配；递归处理嵌套结构。"""
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for k, v in payload.items():
            # RESULT_SINK 下 content 字段透传，避免误删工具正常输出
            if sink == RESULT_SINK and k == "content":
                out[k] = _neutralize_content(v)
                continue
            if _is_sensitive_key(k):
                out[k] = _REDACTED
                continue
            if isinstance(v, dict):
                out[k] = redact_payload(v, sink=sink)
            elif isinstance(v, list):
                out[k] = [redact_payload(item, sink=sink) if isinstance(item, (dict, list, str)) else item for item in v]
            elif isinstance(v, str):
                out[k] = _scan_string(_redact_string(v, sink=sink))
            else:
                out[k] = v
        return out
    elif isinstance(payload, list):
        return [redact_payload(item, sink=sink) for item in payload]
    elif isinstance(payload, str):
        return _scan_string(_redact_string(payload, sink=sink))
    else:
        return payload


def redact_tool_result(result: Any, sink: str = RESULT_SINK) -> Any:
    """工具结果脱敏别名，默认走 RESULT_SINK 宽松策略。"""
    return redact_payload(result, sink=sink)
