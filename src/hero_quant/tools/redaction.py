"""工具结果脱敏与截断：FC 限流与敏感信息过滤。

位于 tools 层输出侧，封装 security.redaction（sink-aware）与 config.limits
截断；超长结果在 TOOL_RESULT_LIMIT（10k）处截断并追加 TRUNCATED 标记，
trace sidecar（50k）走 agent/trace.py 独立路径。
"""

from __future__ import annotations

import json
from typing import Any

from hero_quant.config.limits import TOOL_RESULT_LIMIT, truncate_tool_result


def _maybe_redact(value: Any, sink: str = "result") -> Any:
    """委托 security.redaction 做 sink 感知的脱敏，失败则透传原值。"""
    try:
        from hero_quant.security.redaction import redact_payload

        if isinstance(value, (dict, list)):
            return redact_payload(value, sink=sink)
        if isinstance(value, str):
            # 字符串复用 dict 脱敏路径，需包装后解包
            from hero_quant.security.redaction import redact_payload as rp

            wrapped = {"_v": value}
            redacted = rp(wrapped, sink=sink)
            if redacted.get("_v") == "***":
                return "***"
            return redacted.get("_v", value)
    except Exception:
        pass
    return value


def redact_tool_result(result: Any, limit: int | None = None, sink: str = "result") -> str:
    """先脱敏后截断，保证返回长度受限且敏感信息已被过滤。

    str 先做模式脱敏再截断；dict/list 先做 sink-aware 脱敏再序列化；
    超限时由 truncate_tool_result 追加 TRUNCATED 标记且保证 len <= limit。
    """
    lim = limit if limit is not None else TOOL_RESULT_LIMIT

    # 第一步：脱敏（sink-aware）
    if isinstance(result, (dict, list)):
        redacted = _maybe_redact(result, sink=sink)
        # 序列化后统一按字符串预算做截断
        try:
            s = json.dumps(redacted, ensure_ascii=False)
        except Exception:
            s = str(redacted)
    elif isinstance(result, str):
        r = _maybe_redact(result, sink=sink)
        s = r if isinstance(r, str) else str(r)
    else:
        try:
            s = json.dumps(result, ensure_ascii=False)
        except Exception:
            s = str(result)
        s = _maybe_redact(s, sink=sink) if isinstance(s, str) else s
        if not isinstance(s, str):
            s = str(s)

    # 第二步：超限截断并追加标记
    if len(s) <= lim:
        return s
    return truncate_tool_result(s, limit=lim)
