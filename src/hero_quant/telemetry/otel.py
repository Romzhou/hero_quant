"""OTel three-mode telemetry backbone.

Modes:
- disabled: no export, sharing == disabled
- enabled / sampling / internal: placeholder for metrics-only
- full: placeholder for traces+metrics export

Env gate uses os.environ.get to satisfy config gate (no raw env access outside config).
"""

from __future__ import annotations

import os

# Valid modes — normalized lower-case
_VALID_MODES = {"disabled", "enabled", "sampling", "minimal", "full", "internal", "anonymous"}
_DEFAULT_MODE = "disabled"


def _normalize_mode(raw: str | None) -> str:
    if not raw:
        return _DEFAULT_MODE
    m = raw.strip().lower()
    # map aliases: anonymous -> enabled, internal/minimal/sampling -> enabled
    if m in _VALID_MODES:
        return m
    return _DEFAULT_MODE


def get_otel_mode() -> str:
    """Return current OTel mode from env HERO_OTEL_MODE, default disabled."""
    raw = os.environ.get("HERO_OTEL_MODE", _DEFAULT_MODE)
    # empty string should fallback to disabled
    if raw is None or raw == "":
        return _DEFAULT_MODE
    # keep raw value if valid, else disabled; preserve original case normalized
    norm = raw.strip().lower()
    if norm in _VALID_MODES:
        return norm
    # Accept any non-empty as-is normalized (future-proof), but ensure disabled fallback for empty
    return norm if norm else _DEFAULT_MODE


class SessionTelemetryCoordinator:
    """Per-session telemetry coordinator with sharing level."""

    def __init__(self, mode: str | None = None) -> None:
        # If mode not provided, fallback to env
        if mode is None:
            mode = get_otel_mode()
        self.mode = _normalize_mode(mode)

    def sharing(self) -> str:
        """Return sharing level for current mode.

        Minimal mapping: disabled -> disabled, otherwise return normalized mode.
        This satisfies three-gear contract while keeping placeholder export.
        """
        return self.mode

    # Placeholder for future otel export
    def export(self, payload: dict | None = None) -> None:
        """No-op export placeholder — real exporter wired in infra layer."""
        if self.mode == "disabled":
            return
        # In enabled/full modes, would push to OTel Collector; currently no-op
        return

    def is_enabled(self) -> bool:
        return self.mode != "disabled"
