"""hero_quant.api.middleware — Trace / 安全头 / 审计 / PII 中间件包。"""

from hero_quant.api.middleware.audit import AuditLogger, audit_logger
from hero_quant.api.middleware.pii import (
    CRYPTO_AVAILABLE,
    is_pii_field,
    mask_pii,
    pii_decrypt,
    pii_encrypt,
    safe_log_args,
)
from hero_quant.api.middleware.security_headers import DEFAULT_SECURITY_HEADERS, SecurityHeadersMiddleware
from hero_quant.api.middleware.trace import (
    TraceIdMiddleware,
    clear_trace_id,
    get_request_id,
    get_trace_id,
    set_trace_id,
)

__all__ = [
    "AuditLogger",
    "audit_logger",
    "CRYPTO_AVAILABLE",
    "is_pii_field",
    "mask_pii",
    "pii_decrypt",
    "pii_encrypt",
    "safe_log_args",
    "DEFAULT_SECURITY_HEADERS",
    "SecurityHeadersMiddleware",
    "TraceIdMiddleware",
    "clear_trace_id",
    "get_request_id",
    "get_trace_id",
    "set_trace_id",
]
