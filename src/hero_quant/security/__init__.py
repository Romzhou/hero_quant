"""安全能力入口 — 汇集凭据解析、审批与脱敏三大支柱，对外提供统一导入面。"""

from .approval import ApprovalPolicy, ApprovalService
from .credentials import REF_PATTERN, resolve
from .redaction import redact_payload

__all__ = ["ApprovalPolicy", "ApprovalService", "REF_PATTERN", "resolve", "redact_payload"]
