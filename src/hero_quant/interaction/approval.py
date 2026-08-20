"""Interaction approval — reuses A4 ask→guard three-stage + interrupt + Store isolation.

A4 security approval provides ASK|NEVER folding + never short-circuit.
This module re-exports and adds interaction-layer guard (ask card) + Command resume.
Store (tenant,thread) isolation placeholder.
"""

from __future__ import annotations

from typing import Any

# Reuse A4 security backbone
from hero_quant.security.approval import (
    ApprovalPolicy,
    ApprovalService as SecurityApprovalService,
    effectiveApprovalPolicy,
)

from .questions import AskCardInterrupt, Command


class ApprovalService(SecurityApprovalService):
    """Interaction-layer approval — ask→guard three-stage.

    Extends security ApprovalService with interaction interrupt semantics.
    """

    def request_sync_with_guard(self, tool: str, reason: str | None = None, questions: list[Any] | None = None) -> str:
        # Stage 1: ask card if needed (when mode ask)
        if self.mode == ApprovalPolicy.ASK and questions:
            # Would trigger UserQuestionService ask; placeholder raises interrupt
            raise AskCardInterrupt(questions, reason="ask guard interrupt")
        # Stage 2: guard check
        outcome = self.request_sync(tool=tool, reason=reason)
        # Stage 3: audit via Command resume placeholder
        _ = Command(resume=outcome, goto="decided")
        return outcome


__all__ = ["ApprovalService", "ApprovalPolicy", "effectiveApprovalPolicy", "AskCardInterrupt", "Command"]
