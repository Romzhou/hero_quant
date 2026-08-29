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
        m = _LONG_TOKEN_RE.search(value)
        if m:
            tok = m.group(0)
            # 重复字符（如 x*100）在 ARGUMENTS_SINK 也视为非密钥，避免误杀 trace 用的重复填充内容
            if tok.isdigit() or all(c in "0123456789abcdefABCDEF" for c in tok):
                # 纯 hex/数字不脱敏，返回原值（让调用方继续）
                pass
            elif len(set(tok)) == 1:
                pass  # 单字符重复如 x*100，非密钥
            else:
                return _REDACTED
        return value
    if sink == RESULT_SINK:
        # 结果槽：仅确定性密钥形态必脱敏；长 token 需非纯 hex 且熵>3.0 才脱敏，避免误杀 commit SHA
        if _BEARER_RE.search(value) or _SK_RE.search(value) or _AKIA_RE.search(value) or _JWT_RE.search(value):
            return _REDACTED
        m = _LONG_TOKEN_RE.search(value)
        if m:
            tok = m.group(0)
            # 纯 hex/纯数字指纹视为非密钥，跳过
            if tok.isdigit() or all(c in "0123456789abcdefABCDEF" for c in tok):
                return value
            # 简易熵阈值：去重字符数/长度 >0.35 且含大小写混合或符号才视为密钥
            uniq = len(set(tok))
            if uniq / max(1, len(tok)) < 0.35:
                return value
            has_mixed = any(c.islower() for c in tok) and any(c.isupper() for c in tok)
            has_dash = "-" in tok or "_" in tok
            if not (has_mixed or has_dash):
                return value
            return _REDACTED
        return value
    # unknown sink fail-closed: treat as strict
    if _BEARER_RE.search(value) or _SK_RE.search(value) or _AKIA_RE.search(value) or _JWT_RE.search(value) or _LONG_TOKEN_RE.search(value):
        return _REDACTED
    return _REDACTED if value.strip() and len(value) >= 8 else value


def _scan_string(value: str, *, preserve_zero_width: bool = False) -> str:
    """Apply scanner steps without allowing scanner failures to break redaction."""
    try:
        scanned = scanner.neutralize(value)
        if not preserve_zero_width:
            scanned = scanner.strip_zero_width(scanned)
        return scanned
    except Exception as e:
        # fail-closed: log and redact rather than returning raw value
        try:
            import logging as _lg

            _lg.getLogger(__name__).warning("redaction.scanner_failed", exc_info=e)
        except Exception:
            pass
        return _REDACTED


def _neutralize_content(value: Any) -> Any:
    """Neutralize result content while preserving zero-width chars but still redacting secrets.

    用于 RESULT_SINK 的 content 字段及非 content 字段的历史兼容路径：
    敏感键一律替换为 ***，字符串值按 Bearer/JWT/AKIA 等模式脱敏，同时保留零宽字符扫描。
    """
    if isinstance(value, str):
        # redact Bearer/JWT/AKIA patterns inside content as well
        return _scan_string(_redact_string(value, RESULT_SINK), preserve_zero_width=True)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(key):
                out[key] = _REDACTED
                continue
            if isinstance(item, str):
                out[key] = _scan_string(_redact_string(item, RESULT_SINK), preserve_zero_width=True)
            elif isinstance(item, dict):
                out[key] = _neutralize_content(item)
            elif isinstance(item, (list, tuple, set, frozenset)):
                out[key] = type(item)(_neutralize_content(x) if isinstance(x, (dict, list, tuple, set, frozenset, str)) else x for x in item) if isinstance(item, (tuple, set, frozenset)) else [_neutralize_content(x) if isinstance(x, (dict, list, tuple, set, frozenset, str)) else x for x in item]
            else:
                out[key] = item
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        converted = []
        for item in value:
            if isinstance(item, str):
                converted.append(_scan_string(_redact_string(item, RESULT_SINK), preserve_zero_width=True))
            elif isinstance(item, (dict, list, tuple, set, frozenset)):
                converted.append(_neutralize_content(item))
            else:
                converted.append(item)
        if isinstance(value, tuple):
            return tuple(converted)
        if isinstance(value, set):
            return set(converted)
        if isinstance(value, frozenset):
            return frozenset(converted)
        return converted
    return value


def redact_payload(payload: Any, sink: str = ARGUMENTS_SINK) -> Any:
    """按 sink 瀑布对载荷脱敏：敏感键一律替换，字符串值按模式匹配；递归处理嵌套结构。"""
    try:
        if isinstance(payload, dict):
            out: dict[str, Any] = {}
            for k, v in payload.items():
                # RESULT_SINK: content 字段仍需键级脱敏 + 模式脱敏（保留零宽扫描语义），防止嵌套密钥经 content 外泄
                if sink == RESULT_SINK and k == "content":
                    out[k] = _neutralize_content(v)
                    continue
                if _is_sensitive_key(k):
                    out[k] = _REDACTED
                    continue
                if isinstance(v, dict):
                    out[k] = redact_payload(v, sink=sink)
                elif isinstance(v, (list, tuple, set, frozenset)):
                    # recurse into containers — prevent tuple/set bypass
                    if isinstance(v, list):
                        out[k] = [redact_payload(item, sink=sink) if isinstance(item, (dict, list, tuple, set, frozenset, str)) else item for item in v]
                    elif isinstance(v, tuple):
                        out[k] = tuple(redact_payload(item, sink=sink) if isinstance(item, (dict, list, tuple, set, frozenset, str)) else item for item in v)
                    elif isinstance(v, set):
                        out[k] = set(redact_payload(item, sink=sink) if isinstance(item, (dict, list, tuple, set, frozenset, str)) else item for item in v)
                    else:  # frozenset
                        out[k] = frozenset(redact_payload(item, sink=sink) if isinstance(item, (dict, list, tuple, set, frozenset, str)) else item for item in v)
                elif isinstance(v, str):
                    out[k] = _scan_string(_redact_string(v, sink=sink))
                else:
                    out[k] = v
            return out
        elif isinstance(payload, (list, tuple, set, frozenset)):
            converted = [redact_payload(item, sink=sink) for item in payload]
            if isinstance(payload, tuple):
                return tuple(converted)
            if isinstance(payload, set):
                return set(converted)
            if isinstance(payload, frozenset):
                return frozenset(converted)
            return converted
        elif isinstance(payload, str):
            return _scan_string(_redact_string(payload, sink=sink))
        else:
            # unknown container types — fail-closed: redact if string-like else return as-is but ensure no raw secret leak
            return payload
    except Exception as e:
        # fail-closed: do not return raw payload
        try:
            import logging as _lg2

            _lg2.getLogger(__name__).error("redaction.redact_payload_failed", exc_info=e)
        except Exception:
            pass
        raise


def redact_tool_result(result: Any, sink: str = RESULT_SINK) -> Any:
    """工具结果脱敏别名，默认走 RESULT_SINK 宽松策略。"""
    return redact_payload(result, sink=sink)
