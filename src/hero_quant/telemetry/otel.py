"""OTel three-mode telemetry backbone.

Modes:
- disabled: no export, sharing == disabled
- shared: anonymized metrics export (legacy enabled/sampling/internal/minimal/anonymous map here)
- private: full traces+metrics export (legacy full map here)

Env gates use os.environ.get (config gate pattern). Offline stays green via try/except.
"""

from __future__ import annotations

import os

# Valid modes — normalized lower-case; includes legacy aliases for backwards compat
_VALID_MODES = {"disabled", "shared", "private", "enabled", "sampling", "minimal", "full", "internal", "anonymous"}
_DEFAULT_MODE = "disabled"

# sharing mapping to canonical three gears
_SHARING_MAP = {
    "disabled": "disabled",
    "shared": "shared",
    "private": "private",
    # legacy aliases
    "enabled": "shared",
    "sampling": "shared",
    "minimal": "shared",
    "internal": "shared",
    "anonymous": "shared",
    "full": "private",
}


def _normalize_mode(raw: str | None) -> str:
    if not raw:
        return _DEFAULT_MODE
    m = raw.strip().lower()
    if m in _VALID_MODES:
        return m
    return _DEFAULT_MODE


def get_otel_mode() -> str:
    """Return current OTel mode from env HERO_OTEL_MODE, default disabled."""
    raw = os.environ.get("HERO_OTEL_MODE", _DEFAULT_MODE)
    # empty string should fallback to disabled
    if raw is None or raw == "":
        return _DEFAULT_MODE
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

        Canonical three gears: disabled / shared / private.
        Legacy modes map via _SHARING_MAP.
        """
        return _SHARING_MAP.get(self.mode, "disabled" if self.mode == "disabled" else "shared")

    def export(self, payload: dict | None = None) -> None:
        """Export stub: if mode != disabled tries OTLP endpoint, else no-op. Offline-safe."""
        if self.mode == "disabled":
            return
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if not endpoint:
            return
        try:
            # Best-effort OTLP HTTP export; must not break offline/tests
            import json
            import urllib.request

            data = json.dumps(payload or {}).encode("utf-8")
            req = urllib.request.Request(endpoint, data=data, headers={"Content-Type": "application/json"})
            # short timeout to keep offline green and not block request path
            with urllib.request.urlopen(req, timeout=0.5) as _resp:  # noqa: S310
                pass
        except Exception:
            # offline / no collector / network error -> silent no-op
            return
        return

    def is_enabled(self) -> bool:
        return self.mode != "disabled"
