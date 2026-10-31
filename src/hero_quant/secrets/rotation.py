"""PR-151 轮换记账 —— 只记「版本 / 时间 / 指纹」，绝不记密钥值。

状态机（三态）：
    unknown ──begin_rotation──> rotating ──complete_rotation──> stable
      (无记录)                  (双读窗口)                    (新版本生效)

- `begin_rotation`：要求 `next` 槽（env 后端 = `<VAR>_NEXT`）已配好新值，且指纹与当前值不同；
  只开窗口、不动版本。
- `complete_rotation`：要求当前主槽的值**确实已经切换**到窗口期新值（指纹比对）才记账；
  版本 +1、写 `rotated_at` 与 `previous_version`。切没切由 env 说话，不许口头报完成。
- 状态落 `var/secrets_state.json`（可用参数覆盖，**不新增第二个 env**）。
  不可持久化时 `persistence="memory"` 并如实披露，绝不假装已落盘。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .base import SLOT_CURRENT, SLOT_NEXT, SecretValue, SecretsProvider
from .manager import get_secrets_provider

logger = logging.getLogger(__name__)
try:  # 与 src/hero_quant/data/registry.py 同款：structlog 可选，stdlib logging 兜底（caplog 可断言）
    import structlog as _structlog  # type: ignore

    _slog = _structlog.get_logger(__name__)
except ImportError:  # pragma: no cover — 最小环境无 structlog
    _slog = None  # type: ignore[assignment]

DEFAULT_STATE_PATH = Path("var/secrets_state.json")

STATE_UNKNOWN = "unknown"  # 无记录：从未轮换
STATE_ROTATING = "rotating"  # 双读窗口：新旧并存
STATE_STABLE = "stable"  # 新版本生效，旧版本待撤（撤旧见 docs runbook）

# 状态记录字段白名单：任何密钥值（或可反推值的字段）都不在列
STATE_KEYS = (
    "name",
    "state",
    "version",
    "rotated_at",
    "previous_version",
    "persistence",
    "fingerprint",
    "pending_fingerprint",
)


class SecretRotationError(RuntimeError):
    """轮换前置条件不满足。fail-closed：拒绝记账，不许"口头完成"轮换。"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log_event(event: str, **fields: object) -> None:
    """轮换事件日志：只带 name/version/fingerprint 类字段，绝不传值。"""
    logger.info("secrets.rotation.%s %s", event, fields)
    if _slog is not None:  # pragma: no cover — 有 structlog 时双写
        _slog.info("secrets.rotation.%s", event, **fields)


class RotationStore:
    """轮换状态持久化。写不进去就如实降级为 memory，不假装落盘。"""

    def __init__(self, path: Path | str = DEFAULT_STATE_PATH) -> None:
        self._path = Path(path)
        self._memory: dict[str, dict[str, object]] = {}
        self._persistence = self._probe_persistence()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def persistence(self) -> str:
        return self._persistence

    def _probe_persistence(self) -> str:
        parent = self._path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            if not os.access(str(parent), os.W_OK):
                return "memory"
        except OSError as e:  # 收窄：只接目录/权限类错误，其它异常不吞
            logger.warning("secrets state dir unusable %s: %s", parent, e)
            return "memory"
        return "file"

    def _downgrade(self, reason: BaseException) -> None:
        if self._persistence != "memory":
            logger.warning("secrets state persistence downgraded to memory: %s", reason)
        self._persistence = "memory"

    def get(self, name: str) -> dict[str, object] | None:
        rec = self._load().get(name)
        return dict(rec) if isinstance(rec, dict) else None

    def put(self, record: dict[str, object]) -> None:
        data = self._load()
        data[str(record["name"])] = dict(record)
        self._memory[str(record["name"])] = dict(record)
        if self._persistence == "memory":
            return
        try:
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as e:
            self._downgrade(e)

    def _load(self) -> dict[str, object]:
        if self._persistence == "memory":
            return dict(self._memory)
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return dict(self._memory)
        except OSError as e:
            self._downgrade(e)
            return dict(self._memory)
        try:
            data = json.loads(text) if text.strip() else {}
        except ValueError as e:  # JSONDecodeError 子类
            self._downgrade(e)
            return dict(self._memory)
        if not isinstance(data, dict):
            self._downgrade(ValueError("state root is not an object"))
            return dict(self._memory)
        return data


def _blank(name: str, persistence: str) -> dict[str, object]:
    return {
        "name": name,
        "state": STATE_UNKNOWN,
        "version": 0,
        "rotated_at": None,
        "previous_version": None,
        "persistence": persistence,
        "fingerprint": None,
        "pending_fingerprint": None,
    }


def _status(store: RotationStore, name: str) -> dict[str, object]:
    rec = store.get(name) or _blank(name, store.persistence)
    rec = {k: rec.get(k) for k in STATE_KEYS}
    # persistence 取实时值：落盘失败后如实显示 memory，不沿用旧记录里的 "file"
    rec["persistence"] = store.persistence
    return rec


def rotation_status(name: str, *, state_path: Path | str = DEFAULT_STATE_PATH) -> dict[str, object]:
    """轮换状态（不含任何密钥值）。无记录 → state=unknown / version=0。"""
    return _status(RotationStore(state_path), name)


def begin_rotation(
    name: str,
    *,
    provider: SecretsProvider | None = None,
    state_path: Path | str = DEFAULT_STATE_PATH,
) -> dict[str, object]:
    """开启双读窗口：要求 `next` 槽已配新值，且与当前值指纹不同。不改版本。"""
    prov = provider or get_secrets_provider()
    store = RotationStore(state_path)
    current = prov.get_secret(name, slot=SLOT_CURRENT)
    if current is None:
        raise SecretRotationError(f"cannot begin rotation for {name!r}: current slot has no value")
    pending = prov.get_secret(name, slot=SLOT_NEXT)
    if pending is None:
        raise SecretRotationError(
            f"cannot begin rotation for {name!r}: next slot is empty "
            f"(env backend needs <VAR>_NEXT to be set by the operator; this module never writes secrets)"
        )
    if pending.fingerprint == current.fingerprint:
        raise SecretRotationError(
            f"cannot begin rotation for {name!r}: next value fingerprint equals current "
            f"({pending.fingerprint!r}); refusing to record a no-op rotation"
        )
    rec = store.get(name) or _blank(name, store.persistence)
    rec.update(
        state=STATE_ROTATING,
        pending_fingerprint=pending.fingerprint,
        fingerprint=current.fingerprint,
    )
    store.put(rec)
    _log_event("begin", name=name, state=STATE_ROTATING, **_safe_fields(pending))
    return _status(store, name)


def complete_rotation(
    name: str,
    *,
    provider: SecretsProvider | None = None,
    state_path: Path | str = DEFAULT_STATE_PATH,
) -> dict[str, object]:
    """完成轮换：主槽必须真的已是窗口期新值（指纹比对），才版本 +1。"""
    prov = provider or get_secrets_provider()
    store = RotationStore(state_path)
    rec = store.get(name)
    if rec is None or rec.get("state") != STATE_ROTATING:
        raise SecretRotationError(
            f"cannot complete rotation for {name!r}: no rotation in progress "
            f"(state={rec.get('state') if rec else STATE_UNKNOWN!r}); call begin_rotation first"
        )
    current = prov.get_secret(name, slot=SLOT_CURRENT)
    if current is None:
        raise SecretRotationError(f"cannot complete rotation for {name!r}: current slot lost its value")
    if current.fingerprint != rec.get("pending_fingerprint"):
        raise SecretRotationError(
            f"cannot complete rotation for {name!r}: current value fingerprint {current.fingerprint!r} "
            f"!= pending {rec.get('pending_fingerprint')!r}; the new value has not been promoted yet"
        )
    previous_version = int(rec.get("version") or 0)
    rec.update(
        state=STATE_STABLE,
        version=previous_version + 1,
        previous_version=previous_version,
        rotated_at=_now(),
        fingerprint=current.fingerprint,
        pending_fingerprint=None,
    )
    store.put(rec)
    _log_event("complete", name=name, state=STATE_STABLE, **_safe_fields(current))
    return _status(store, name)


def _safe_fields(secret: SecretValue) -> dict[str, object]:
    """日志字段：指纹/长度/槽位/后端，**不含 name 之外的任何可反推值的信息**（与 _log_event 的 name 不撞键）。"""
    return {
        "backend": secret.backend,
        "slot": secret.slot,
        "length": secret.length,
        "fingerprint": secret.fingerprint,
    }
