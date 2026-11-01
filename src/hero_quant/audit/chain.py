"""audit.chain — 链式哈希追加与校验（PR-241）。

职责：
- 每次追加在**一个事务**里：锁 audit_chain_head（SELECT ... FOR UPDATE）→ 算 entry_hash
  → INSERT audit_log → UPSERT 链头；并发追加因此串行化，不会分叉。
- verify 按 id 升序重算，返回首个断点（hash_mismatch / prev_mismatch / head_mismatch）
  而**不是**抛异常；链外历史行（entry_hash IS NULL）不回填，只统计 uncovered_count，
  链从 chain_start_entry_id 起算。

规范化口径（entry_hash 的输入，顺序固定，不含自增 id —— INSERT 前未知）::

    ts | tenant_id | action | resource | run_id | model | prompt_hash | result_hash | ip | detail

- ts 统一折算成 UTC 的 %Y-%m-%dT%H:%M:%S.%f：写入时用 Python 侧生成的时间，
  校验时读回 timestamptz 再折算，两端必须同值（见 tests/test_pr241_audit_chain.py）。
- detail 用 canonical JSON（sort_keys=True、无多余空白、ensure_ascii=False）：
  jsonb 会重排键，只有规范化后才能跨读写稳定。
- 空值统一写成空串（None 与 "" 同义），避免 NULL 与空串两套语义。

约束：本模块只写不删；audit_log 的 append-only 触发器（0003）拒绝 UPDATE/DELETE，
因此哈希必须在 INSERT 前算完，绝不做事后回填。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any, Mapping

from hero_quant.audit.store import (
    AuditStore,
    AuditValidationError,
    _MAX_HASH_LEN,
    _MAX_TENANT_LEN,
    _MAX_TEXT_LEN,
    _text,
)
from hero_quant.db.rows import execute, fetch_all, fetch_one

#: 空链的起点：64 个 0（与 sha256 同宽的 hex 串）。
GENESIS_HASH = "0" * 64

#: 规范化字段顺序；改顺序 = 改链的语义，必须同步迁移与契约。
_HASH_FIELDS: tuple[str, ...] = (
    "ts",
    "tenant_id",
    "action",
    "resource",
    "run_id",
    "model",
    "prompt_hash",
    "result_hash",
    "ip",
    "detail",
)

#: verify 单次最多扫的行数（防一次请求拖垮 PG）；API 层另有更小的上限。
_MAX_VERIFY_LIMIT = 10_000
_UNBOUNDED_LIMIT = 2_147_483_647

_HEAD_COLUMNS = "head_id, head_hash, entries, updated_at"
_AUDIT_COLUMNS = (
    "id, ts, tenant_id, user_id, action, resource, run_id, model, prompt_hash, "
    "result_hash, ip, detail, prev_hash, entry_hash"
)

_HEAD_LOCK_SQL = f"SELECT {_HEAD_COLUMNS} FROM audit_chain_head WHERE tenant_id = %s FOR UPDATE"
_HEAD_READ_SQL = f"SELECT {_HEAD_COLUMNS} FROM audit_chain_head WHERE tenant_id = %s"
# 链头是派生状态：没有行时用 GENESIS 起算；首次追加锁不到行，用事务级 advisory lock
# 串行化，否则两个并发首写会各按 GENESIS 造出两条互不相连的「第一条」。
_HEAD_BOOTSTRAP_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"
_HEAD_UPSERT_SQL = (
    "INSERT INTO audit_chain_head (tenant_id, head_id, head_hash, entries, updated_at) "
    "VALUES (%s, %s, %s, %s, now()) "
    "ON CONFLICT (tenant_id) DO UPDATE SET head_id = EXCLUDED.head_id, "
    "head_hash = EXCLUDED.head_hash, entries = EXCLUDED.entries, updated_at = now() "
    f"RETURNING {_HEAD_COLUMNS}"
)
_APPEND_SQL = (
    "INSERT INTO audit_log (ts, tenant_id, user_id, action, resource, run_id, model, "
    "prompt_hash, result_hash, ip, detail, prev_hash, entry_hash) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s) "
    f"RETURNING {_AUDIT_COLUMNS}"
)
_NO_HEAD: dict[str, Any] = {"head_id": None, "head_hash": GENESIS_HASH, "entries": 0, "updated_at": None}


def _canonical_ts(value: Any) -> str:
    """时间统一折算成 UTC 的 %Y-%m-%dT%H:%M:%S.%f（微秒固定 6 位）。

    中文：写入端拿 Python datetime、校验端拿 timestamptz 转成的 ISO 串，
    两边时区表示不同（PG 按会话 TimeZone 回显），必须先归一到 UTC 才能同值。
    """
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
    if isinstance(value, date):
        return f"{value.isoformat()}T00:00:00.000000"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            return _canonical_ts(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            return text  # 非 ISO 串原样入链（不猜语义，保证两端一致）
    if value is None:
        return ""
    return str(value)


def _stringify_keys(value: Any) -> Any:
    """递归把 mapping 的键转成 str：json.dumps 允许 int 键，但读回后键恒为 str。"""
    if isinstance(value, Mapping):
        return {str(key): _stringify_keys(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stringify_keys(item) for item in value]
    return value


def _canonical_detail(value: Any) -> str:
    """canonical JSON：键有序、无空白、不转义非 ASCII；None 视同空对象。"""
    payload = {} if value is None else value
    return json.dumps(
        _stringify_keys(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _canonical_field(field: str, value: Any) -> str:
    if field == "ts":
        return _canonical_ts(value)
    if field == "detail":
        return _canonical_detail(value)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _check_prev_hash(prev_hash: str) -> str:
    if not isinstance(prev_hash, str) or len(prev_hash) != 64:
        raise ValueError("prev_hash_must_be_64_hex_chars")
    return prev_hash.lower()


def entry_hash(prev_hash: str, row: Mapping[str, Any]) -> str:
    """sha256(prev_hash | ts | tenant_id | ... | detail)；同一行两次调用恒同值。

    入参 row 可以是「待写入的字段字典」，也可以是「从 audit_log 读回的行」——
    两者只要字段值等价（时间同一瞬间、detail 同一结构）就必须得出同一 hash。
    自增 id 故意不参与：INSERT 前拿不到，事后回填又会破坏 append-only 语义。
    """
    previous = _check_prev_hash(prev_hash)
    parts = [previous]
    parts.extend(_canonical_field(field, row.get(field)) for field in _HASH_FIELDS)
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _coerce_ts(value: Any) -> datetime:
    """把 ts 入参落成 tz-aware datetime（缺省 now）；非法格式在取连接前拒绝。"""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AuditValidationError("ts_must_be_iso_datetime") from exc
        return _coerce_ts(parsed)
    raise AuditValidationError("ts_must_be_datetime_or_iso_string")


def _clamp_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise AuditValidationError("limit_must_be_int")
    return max(1, min(limit, _MAX_VERIFY_LIMIT))


class AuditChain:
    """AuditStore 之上的链式哈希追加/校验；不新建连接池，复用 store 的事务形态。"""

    def __init__(self, store: AuditStore) -> None:
        if store is None or not callable(getattr(store, "_tx", None)):
            raise ValueError("store must be an AuditStore (provides ._tx)")
        self._store = store

    # ------------------------------------------------------------------
    # 追加
    # ------------------------------------------------------------------
    def append(self, tenant_id: str, action: str, **fields: Any) -> dict[str, Any]:
        """追加一条**已入链**的审计，返回落库行（含 prev_hash/entry_hash）。

        字段集与 AuditStore.append 一致，额外接受 ts（仅用于确定性测试/补录，
        缺省取当前 UTC 时间）。校验全部在取连接前完成：非法入参得 400 而不是 503。
        """
        tenant = _text(tenant_id, "tenant_id", _MAX_TENANT_LEN, required=True)
        act = _text(action, "action", _MAX_TEXT_LEN, required=True)
        detail_value = fields.pop("detail", None)
        payload = AuditStore._safe_detail(detail_value)
        actor_id = _text(fields.pop("user_id", None), "user_id", _MAX_TEXT_LEN, required=False)
        resource = _text(fields.pop("resource", None), "resource", _MAX_TEXT_LEN, required=False)
        run = _text(fields.pop("run_id", None), "run_id", _MAX_TEXT_LEN, required=False)
        model = _text(fields.pop("model", None), "model", _MAX_TEXT_LEN, required=False)
        prompt_digest = _text(fields.pop("prompt_hash", None), "prompt_hash", _MAX_HASH_LEN, required=False)
        result_digest = _text(fields.pop("result_hash", None), "result_hash", _MAX_HASH_LEN, required=False)
        client_ip = _text(fields.pop("ip", None), "ip", 64, required=False)
        moment = _coerce_ts(fields.pop("ts", None))
        if fields:
            # 未知字段直接拒绝：静默丢弃会让调用方误以为入链了（审计最忌讳假成功）。
            raise AuditValidationError(f"unknown_audit_fields:{','.join(sorted(fields))}")

        row = {
            "ts": moment,
            "tenant_id": tenant,
            "action": act,
            "resource": resource,
            "run_id": run,
            "model": model,
            "prompt_hash": prompt_digest,
            "result_hash": result_digest,
            "ip": client_ip,
            "detail": {} if detail_value is None else detail_value,
        }

        with self._store._tx(tenant) as conn:
            head = fetch_one(conn, _HEAD_LOCK_SQL, (tenant,))
            if head is None:
                # 首次追加：行还不存在，FOR UPDATE 锁不住，用事务级 advisory lock 防首写分叉。
                execute(conn, _HEAD_BOOTSTRAP_LOCK_SQL, (f"hero_quant:audit_chain:{tenant}",))
                head = fetch_one(conn, _HEAD_LOCK_SQL, (tenant,))
            prev_hash = GENESIS_HASH if head is None else str(head["head_hash"])
            digest = entry_hash(prev_hash, row)
            entries = (int(head["entries"]) if head is not None else 0) + 1

            inserted = fetch_one(
                conn,
                _APPEND_SQL,
                (
                    moment,
                    tenant,
                    actor_id,
                    act,
                    resource,
                    run,
                    model,
                    prompt_digest,
                    result_digest,
                    client_ip,
                    payload,
                    prev_hash,
                    digest,
                ),
            )
            if inserted is None:
                raise AuditValidationError("insert_returned_no_row")
            # 链头是派生状态：与本次 INSERT 同事务提交，杜绝「行在链不在」。
            fetch_one(conn, _HEAD_UPSERT_SQL, (tenant, int(inserted["id"]), digest, entries))
        return inserted

    # ------------------------------------------------------------------
    # 链头 / 校验
    # ------------------------------------------------------------------
    def head(self, tenant_id: str) -> dict[str, Any]:
        """当前链头；从未追加过则返回 GENESIS + entries=0（不伪造行）。"""
        tenant = _text(tenant_id, "tenant_id", _MAX_TENANT_LEN, required=True)
        with self._store._tx(tenant) as conn:
            row = fetch_one(conn, _HEAD_READ_SQL, (tenant,))
        if row is None:
            return dict(_NO_HEAD)
        return row

    def verify(self, tenant_id: str, *, limit: int | None = None) -> dict[str, Any]:
        """按 id 升序重算链，返回首个断点；**绝不**用异常表示篡改。

        返回字段：ok / entries / head_hash / broken_at / reason / degraded / reason_code
        以及覆盖区间 chain_start_entry_id / chain_start_at / uncovered_count。

        - reason：hash_mismatch（行内容被改）/ prev_mismatch（链环断开，典型是中间行
          被删）/ head_mismatch（尾部被截断，链头与扫到的末端不符）。
        - degraded=True 只在「无法断言完整」时给出：limit 截断扫描
          （reason_code="scan_truncated"）或链头行缺失（head_missing）。
        - entries 是本次实际校验的入链行数；head_hash 是扫到的链末端哈希
          （无入链行时为 None，不拿 GENESIS 冒充真实哈希）。
        """
        tenant = _text(tenant_id, "tenant_id", _MAX_TENANT_LEN, required=True)
        cap = None if limit is None else _clamp_limit(limit)

        with self._store._tx(tenant) as conn:
            counted = fetch_one(
                conn,
                "SELECT count(*) AS covered_count FROM audit_log "
                "WHERE tenant_id = %s AND entry_hash IS NOT NULL",
                (tenant,),
            )
            uncovered = fetch_one(
                conn,
                "SELECT count(*) AS uncovered_count FROM audit_log "
                "WHERE tenant_id = %s AND entry_hash IS NULL",
                (tenant,),
            )
            start = fetch_one(
                conn,
                "SELECT id, ts FROM audit_log WHERE tenant_id = %s AND entry_hash IS NOT NULL "
                "ORDER BY id ASC LIMIT 1",
                (tenant,),
            )
            rows = fetch_all(
                conn,
                "SELECT id, ts, tenant_id, action, resource, run_id, model, prompt_hash, "
                "result_hash, ip, detail, prev_hash, entry_hash FROM audit_log "
                "WHERE tenant_id = %s AND entry_hash IS NOT NULL ORDER BY id ASC LIMIT %s",
                (tenant, cap if cap is not None else _UNBOUNDED_LIMIT),
            )
            head = fetch_one(conn, _HEAD_READ_SQL, (tenant,))

        covered_count = int(counted["covered_count"]) if counted else 0
        uncovered_count = int(uncovered["uncovered_count"]) if uncovered else 0
        chain_start_entry_id = None if start is None else start["id"]
        chain_start_at = None if start is None else start["ts"]

        prev = GENESIS_HASH
        entries = 0
        broken_at: Any = None
        reason: str | None = None
        for row in rows:
            entries += 1
            if row.get("prev_hash") != prev:
                broken_at, reason = row.get("id"), "prev_mismatch"
                break
            expected = entry_hash(prev, row)
            if expected != row.get("entry_hash"):
                broken_at, reason = row.get("id"), "hash_mismatch"
                break
            prev = expected

        truncated = cap is not None and covered_count > len(rows)
        degraded = False
        reason_code: str | None = None
        ok = reason is None

        if ok and truncated:
            # 只校验了前缀：不能断言整条链完整（fail-closed，不给假绿）。
            ok = False
            degraded = True
            reason_code = "scan_truncated"
        elif ok and covered_count > 0:
            if head is None:
                ok = False
                degraded = True
                reason_code = "head_missing"
            elif (
                int(head["entries"]) != entries
                or head["head_hash"] != prev
                or head["head_id"] != rows[-1]["id"]
            ):
                ok = False
                broken_at, reason = None, "head_mismatch"

        return {
            "ok": ok,
            "entries": entries,
            "head_hash": None if prev == GENESIS_HASH else prev,
            "broken_at": broken_at,
            "reason": reason,
            "degraded": degraded,
            "reason_code": reason_code,
            "chain_start_entry_id": chain_start_entry_id,
            "chain_start_at": chain_start_at,
            "uncovered_count": uncovered_count,
        }


__all__ = ["GENESIS_HASH", "AuditChain", "entry_hash"]
