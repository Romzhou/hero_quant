"""PR-151 轮换事件 → 审计链桥（PR-241）。

`begin_rotation` / `complete_rotation` 返回的状态记录（`name/state/version/fingerprint/...`）
经本模块的 hook 写成一条入链审计：

    secrets.rotation.begin      ← state == "rotating"
    secrets.rotation.complete   ← state == "stable"

指纹口径：begin 记的是轮换**前**指纹（当前槽），complete 记的是轮换**后**指纹（已提升到主槽）；
这一对已经能证明「值确实换了」，因此不再单独记 pending 指纹（contract 裁定，2026-11-01）。

硬约束：
1. **只写白名单字段** `name/version/fingerprint/state`：不写密钥值、长度、pending 指纹等
   任何原始材料；`detail` 仍要过 `AuditStore._safe_detail` 的敏感键黑名单。
2. **审计是旁路**：hook 内部任何异常都吞掉并打 warning，绝不让轮换因为审计不可用而失败
   （与 `audit.producers` 同语义）。
3. **不 import rotation**：状态字面量与 `rotation.STATE_ROTATING/STATE_STABLE` 同值，
   但这里写死常量，避免主控在 `rotation.py` 里 import 本模块时形成循环导入；
   `tests/test_pr151_rotation_audit.py` 锁定两组常量一致。
4. 轮换是进程级动作，没有调用方租户：默认落 `SYSTEM_TENANT`，事件里带
   `tenant_id` 时可覆盖（多租户轮换场景由主控决定是否传）。
5. 本模块不 import server.py / loop.py；接线由主控在批次 4 完成。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

logger = logging.getLogger(__name__)
try:  # 与 secrets/rotation.py 同款：structlog 优先，stdlib 兜底（caplog 可断言）
    import structlog as _structlog  # type: ignore

    _slog = _structlog.get_logger(__name__)
except ImportError:  # pragma: no cover — 最小环境无 structlog
    _slog = None  # type: ignore[assignment]

ACTION_ROTATION_BEGIN = "secrets.rotation.begin"
ACTION_ROTATION_COMPLETE = "secrets.rotation.complete"

#: 轮换事件没有租户上下文时的落点（系统级动作）
SYSTEM_TENANT = "system"

# 与 rotation.STATE_ROTATING / STATE_STABLE 同值（故意不 import，见模块 docstring 第 3 条）
_STATE_ROTATING = "rotating"
_STATE_STABLE = "stable"

_STATE_ACTIONS: Mapping[str, str] = {
    _STATE_ROTATING: ACTION_ROTATION_BEGIN,
    _STATE_STABLE: ACTION_ROTATION_COMPLETE,
}

#: detail 白名单：字段名即契约，新增字段必须同步契约与测试
DETAIL_KEYS = ("name", "version", "fingerprint", "state")


def _event(level: int, name: str, **fields: Any) -> None:
    """旁路日志：stdlib 保证 caplog 可断言，structlog 存在时双写（字段只放 error_code/状态/类型名）。"""
    logger.log(level, "%s %s", name, " ".join(f"{key}={value}" for key, value in fields.items()))
    if _slog is not None:  # pragma: no cover — 有 structlog 时双写
        emit = _slog.warning if level >= logging.WARNING else _slog.info
        emit(name, **fields)


def _detail(event: Mapping[str, Any]) -> dict[str, Any]:
    """只取白名单字段；缺项写 None 表示「未知」，不造 0 / 空串冒充。"""
    return {key: event.get(key) for key in DETAIL_KEYS}


def rotation_audit_hook(chain: Any) -> Callable[[dict], None]:
    """构造 hook：接收 `begin_rotation`/`complete_rotation` 的返回 dict，写审计链。

    用法（主控接线，示意）::

        hook = rotation_audit_hook(AuditChain(get_audit_store()))
        hook(begin_rotation("llm_api_key"))

    - `chain` 只需提供 `append(tenant_id, action, *, resource=..., detail=...)`（鸭子类型，便于单测）；
    - 返回的 hook 恒返回 None，且**永不抛**。
    """

    def _hook(event: Any) -> None:
        try:
            if not isinstance(event, Mapping):
                _event(logging.WARNING, "secrets.rotation_audit_skipped", error_code="rotation_event_invalid")
                return None
            state = event.get("state")
            action = _STATE_ACTIONS.get(state if isinstance(state, str) else "")
            if action is None:
                # unknown / 无状态：不是轮换事件，跳过（state 是白名单值，可入日志）
                _event(
                    logging.INFO,
                    "secrets.rotation_audit_skipped",
                    error_code="rotation_state_not_a_transition",
                    state=state,
                )
                return None
            name = event.get("name")
            if not isinstance(name, str) or not name:
                _event(logging.WARNING, "secrets.rotation_audit_skipped", error_code="rotation_name_missing")
                return None
            tenant = event.get("tenant_id")
            tenant_id = tenant if isinstance(tenant, str) and tenant else SYSTEM_TENANT
            chain.append(tenant_id, action, resource=name, detail=_detail(event))
        except Exception as exc:  # noqa: BLE001 — 审计是旁路：任何链/驱动异常都不得冒到轮换流程
            # 只记异常类型与稳定 error_code：异常文本可能带 DSN / 值，绝不入日志
            _event(
                logging.WARNING,
                "secrets.rotation_audit_failed",
                error_code="rotation_audit_failed",
                error_type=type(exc).__name__,
            )
        return None

    return _hook


__all__ = [
    "ACTION_ROTATION_BEGIN",
    "ACTION_ROTATION_COMPLETE",
    "DETAIL_KEYS",
    "SYSTEM_TENANT",
    "rotation_audit_hook",
]
