"""killswitch — 租户/模型/工具一键停用（W8 PR-173）。"""

from hero_quant.killswitch.switch import (
    KEY_PREFIX,
    SCOPES,
    KillSwitch,
    KillSwitchActive,
    KillSwitchError,
    KillSwitchUnavailable,
    KillSwitchValidationError,
)

__all__ = [
    "KEY_PREFIX",
    "SCOPES",
    "KillSwitch",
    "KillSwitchActive",
    "KillSwitchError",
    "KillSwitchUnavailable",
    "KillSwitchValidationError",
]
