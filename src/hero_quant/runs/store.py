"""runs.store — PG 长任务队列（PR-201/202/203）。

设计（docs/contracts/batch-14.md §2.1/§2.2/§2.4/§5）：

- **一张表**：复用 0002 的 `runs`（0005 增列），不另建第二张 run 表，避免链A
  （session/store.py）与队列两套 run 身份。
- **领取一条语句**：`UPDATE ... FROM (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)`，
  单事务、无长事务；handler 在事务外跑（LLM 调用绝不包在事务里）。
- **fencing**：`finish` / `fail` / `heartbeat` 一律带 `AND attempt = %s`；影响 0 行即
  丢弃写、打 `runs.write_fenced`、返回 False。僵尸 worker 因此写不进结果。
- **RLS/GUC**：跨租户领取只在领取事务内 `SET LOCAL app.queue_worker='on'`（禁止会话级
  SET）；领到后同一事务内立刻 `SET LOCAL app.tenant_id` + 把 worker GUC 清空，执行期与
  写结果回归普通租户 RLS。连接回池不残留 GUC（SET LOCAL 随事务结束失效）。
- **租户解析**：finish/fail/heartbeat 的契约签名不带 tenant_id，RunQueue 在 claim 时把
  `run_id -> (tenant_id, attempt)` 记在进程内；写结果时据此设置租户上下文。重启后的
  worker 会重新 claim（新 attempt），因此不会读到上一次的旧条目。
- **失败语义**：`attempt >= max_tries` 置 dead（与 §5 看门狗口径一致，覆盖 §2.1 的
  "attempt > max_tries" 表述），否则回 queued 等下一次领取。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from hero_quant.db.pool import DatabaseUnavailable, get_pool
from hero_quant.db.rows import execute, fetch_all, fetch_one
from hero_quant.runs.guards import assert_no_queue_worker

try:  # 可选依赖：把池超时映射成 503，而不是 500
    from psycopg_pool import PoolTimeout as _PoolTimeout  # type: ignore
except ImportError:  # pragma: no cover - 正常安装 psycopg_pool 时不会走到
    _PoolTimeout = ()  # type: ignore[assignment]

logger = logging.getLogger("hero_quant.runs")

RUN_ID_BYTES = 12
MAX_TENANT_LEN = 128
MAX_ID_LEN = 128
MAX_KIND_LEN = 32
MAX_ERROR_LEN = 2000
MAX_LIMIT = 200
MAX_PAYLOAD_BYTES = 1_000_000
MIN_LEASE_SECONDS = 1
MAX_LEASE_SECONDS = 86_400
MAX_TRIES_UPPER = 100

KINDS = frozenset({"agent", "backtest", "research"})
RUN_STATUSES = frozenset(
    {"queued", "running", "succeeded", "failed", "cancelled", "cancelling", "dead"}
)
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "dead"})
CANCELLABLE_STATUSES = frozenset({"queued", "running"})

_RUN_COLUMNS = (
    "id, tenant_id, session_id, status, kind, attempt, max_tries, lease_until, heartbeat_at, "
    "payload, result, error, reason, cost, started_at, ended_at, created_at"
)
# UPDATE ... FROM picked 时 picked 里也有 id，RETURNING 必须带表名消歧义。
_RUN_COLUMNS_QUALIFIED = ", ".join(f"runs.{name.strip()}" for name in _RUN_COLUMNS.split(","))

_CLAIM_SQL = (
    "WITH picked AS ("
    " SELECT id FROM runs WHERE status = 'queued' ORDER BY created_at, id"
    " FOR UPDATE SKIP LOCKED LIMIT 1"
    ") "
    "UPDATE runs SET status = 'running', attempt = runs.attempt + 1,"
    " lease_until = now() + make_interval(secs => %s), heartbeat_at = now(),"
    " started_at = COALESCE(runs.started_at, now()), error = NULL"
    " FROM picked WHERE runs.id = picked.id"
    f" RETURNING {_RUN_COLUMNS_QUALIFIED}"
)

_FINISH_SQL = (
    "UPDATE runs SET status = 'succeeded', result = %s::jsonb, error = NULL,"
    " lease_until = NULL, heartbeat_at = now(), ended_at = now()"
    " WHERE id = %s AND attempt = %s AND status = 'running'"
    " RETURNING id"
)

_FAIL_SQL = (
    "UPDATE runs SET"
    " status = CASE WHEN attempt >= max_tries THEN 'dead' ELSE 'queued' END,"
    " error = %s,"
    " ended_at = CASE WHEN attempt >= max_tries THEN now() ELSE NULL END,"
    " lease_until = NULL,"
    " heartbeat_at = now()"
    " WHERE id = %s AND attempt = %s AND status = 'running'"
    " RETURNING status, attempt, max_tries"
)

_HEARTBEAT_SQL = (
    "UPDATE runs SET lease_until = now() + make_interval(secs => %s), heartbeat_at = now()"
    " WHERE id = %s AND attempt = %s AND status = 'running'"
    " RETURNING id"
)

_CANCEL_SQL = (
    "UPDATE runs SET"
    " status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE 'cancelling' END,"
    " ended_at = CASE WHEN status = 'queued' THEN now() ELSE ended_at END"
    " WHERE tenant_id = %s AND id = %s AND status IN ('queued', 'running')"
    f" RETURNING {_RUN_COLUMNS}"
)

_SETTLE_CANCEL_SQL = (
    "UPDATE runs SET status = 'cancelled', lease_until = NULL, ended_at = now()"
    " WHERE id = %s AND attempt = %s AND status = 'cancelling'"
    " RETURNING id"
)

_REAP_DEAD_SQL = (
    "UPDATE runs SET status = 'dead',"
    " error = COALESCE(error, 'lease_expired'),"
    " lease_until = NULL, ended_at = now()"
    " WHERE status = 'running' AND lease_until IS NOT NULL AND lease_until < now()"
    " AND attempt >= max_tries"
    " RETURNING id"
)

_REAP_REQUEUE_SQL = (
    "UPDATE runs SET status = 'queued', lease_until = NULL"
    " WHERE status = 'running' AND lease_until IS NOT NULL AND lease_until < now()"
    " AND attempt < max_tries"
    " RETURNING id"
)


class RunsError(RuntimeError):
    """队列错误基类：reason 是稳定的机器可读码，detail 只进日志。"""

    error = "runs_error"
    status_code = 500

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        message = f"{self.error}: {reason}"
        if detail:
            message += f" ({detail})"
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.error, "reason": self.reason}


class RunsUnavailable(RunsError):
    """无可用 PG（未配置 / 探活失败 / 池超时）—— 503。"""

    error = "runs_unavailable"
    status_code = 503


class RunNotFound(RunsError):
    """运行不存在或不属于该租户 —— 404（跨租户表现成「不存在」）。"""

    error = "run_not_found"
    status_code = 404


class RunsValidationError(RunsError):
    """入参非法 —— 400。"""

    error = "run_invalid_request"
    status_code = 400


# ---------------------------------------------------------------------------
# 入参归一
# ---------------------------------------------------------------------------
def _text(value: Any, field: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise RunsValidationError(f"{field}_must_be_str")
    text = value.strip()
    if not text:
        raise RunsValidationError(f"{field}_must_be_non_empty")
    if len(text) > max_len:
        raise RunsValidationError(f"{field}_too_long")
    return text


def _limit(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RunsValidationError("limit_must_be_int") from exc
    if number <= 0:
        raise RunsValidationError("limit_must_be_positive")
    return min(number, MAX_LIMIT)


def _lease_seconds(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RunsValidationError("lease_seconds_must_be_int") from exc
    if not (MIN_LEASE_SECONDS <= number <= MAX_LEASE_SECONDS):
        raise RunsValidationError("lease_seconds_out_of_range")
    return number


def _max_tries(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RunsValidationError("max_tries_must_be_int") from exc
    if not (1 <= number <= MAX_TRIES_UPPER):
        raise RunsValidationError("max_tries_out_of_range")
    return number


def _attempt(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RunsValidationError("attempt_must_be_int") from exc
    if number < 0:
        raise RunsValidationError("attempt_must_be_non_negative")
    return number


def _encode_json(value: Any, field: str) -> str | None:
    """jsonb 入参编码：None 保持 NULL（不写 'null' 冒充有值）。"""
    if value is None:
        return None
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        raise RunsValidationError(f"{field}_not_json_serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise RunsValidationError(f"{field}_too_large")
    return encoded


def _clip_error(error: Any) -> str:
    text = str(error or "handler_error").strip() or "handler_error"
    return text[:MAX_ERROR_LEN]


# ---------------------------------------------------------------------------
# 连接/事务基类（RunQueue 与 StepLedger 共用）
# ---------------------------------------------------------------------------
class _PoolBacked:
    """持有连接池并按「短事务 + 事务级 GUC」取用；不隐式建池。"""

    def __init__(self, pool: Any) -> None:
        if pool is None or not callable(getattr(pool, "connection", None)):
            raise ValueError("pool must provide a callable .connection()")
        self._pool = pool

    @property
    def pool(self) -> Any:
        return self._pool

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        """取连接并做 worker-GUC 守卫；池/驱动异常一律 503。"""
        try:
            ctx = self._pool.connection()
        except Exception as exc:  # noqa: BLE001 - 取连接失败必须转显式不可用
            raise RunsUnavailable("pool_connection_failed", type(exc).__name__) from exc
        try:
            with ctx as conn:
                assert_no_queue_worker(conn)
                yield conn
        except _PoolTimeout as exc:  # type: ignore[misc]
            raise RunsUnavailable("pool_timeout", type(exc).__name__) from exc

    @contextmanager
    def _tx(self, tenant_id: str) -> Iterator[Any]:
        """单事务 + 事务级租户上下文（RLS 判据）。"""
        tenant = _text(tenant_id, "tenant_id", MAX_TENANT_LEN)
        with self._connection() as conn:
            with conn.transaction():
                execute(conn, "SELECT set_config('app.tenant', %s, true)", (tenant,))
                execute(conn, "SELECT set_config('app.current_tenant', %s, true)", (tenant,))
                yield conn

    @contextmanager
    def _worker_tx(self) -> Iterator[Any]:
        """领取/看门狗专用事务：事务级 worker GUC，提交即失效（绝不会话级 SET）。"""
        with self._connection() as conn:
            with conn.transaction():
                execute(conn, "SELECT set_config('app.queue_worker', 'on', true)")
                yield conn


class RunQueue(_PoolBacked):
    """PG 队列读写方；方法签名见 docs/contracts/batch-14.md §5。"""

    def __init__(self, pool: Any) -> None:
        super().__init__(pool)
        self._claims: dict[str, tuple[str, int]] = {}
        self._claims_lock = threading.Lock()

    @classmethod
    def from_dsn(cls, dsn: str | None = None) -> "RunQueue":
        """从 DSN / Settings.checkpoint_dsn 建池；不可用即 fail-closed。"""
        try:
            return cls(get_pool(dsn))
        except DatabaseUnavailable as exc:
            raise RunsUnavailable(exc.reason, str(exc)) from exc

    # ------------------------------------------------------------- claim 台账
    def _remember_claim(self, run_id: str, tenant_id: str, attempt: int) -> None:
        with self._claims_lock:
            self._claims[run_id] = (tenant_id, attempt)

    def _tenant_for(self, run_id: str) -> str:
        """claim 台账里取租户；没有台账说明这个实例没领过该 run，拒绝盲写。"""
        with self._claims_lock:
            entry = self._claims.get(run_id)
        if entry is None:
            raise RunsError("run_not_claimed", f"run_id={run_id}")
        return entry[0]

    def _claim_tenant(self, run_id: str, op: str, attempt: int) -> str | None:
        """取写结果用的租户；台账没有条目时按「丢弃写」处理（不盲写、不抛给调用方）。"""
        try:
            return self._tenant_for(run_id)
        except RunsError as exc:
            if exc.reason != "run_not_claimed":
                raise
            logger.warning(
                "runs.write_fenced run_id=%s attempt=%s op=%s reason=run_not_claimed"
                " error_code=runs.write_fenced",
                run_id,
                attempt,
                op,
            )
            return None

    def _forget(self, run_id: str, attempt: int | None = None) -> None:
        """清台账（幂等）；attempt 给定时只在仍匹配时清，避免清掉新领取的条目。"""
        with self._claims_lock:
            entry = self._claims.get(run_id)
            if entry is None:
                return
            if attempt is not None and entry[1] != attempt:
                return
            self._claims.pop(run_id, None)

    def claim_registry_size(self) -> int:
        """当前进程持有的 claim 条目数（运维/测试观察用，不参与业务判定）。"""
        with self._claims_lock:
            return len(self._claims)

    # ------------------------------------------------------------------ 写入
    def enqueue(
        self,
        tenant_id: str,
        kind: str,
        payload: Mapping[str, Any],
        *,
        session_id: str | None = None,
        max_tries: int = 3,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """入队；返回新建的 run 行（status='queued'）。"""
        tenant = _text(tenant_id, "tenant_id", MAX_TENANT_LEN)
        kind_text = _text(kind, "kind", MAX_KIND_LEN)
        if kind_text not in KINDS:
            raise RunsValidationError("kind_not_supported")
        if not isinstance(payload, Mapping):
            raise RunsValidationError("payload_must_be_object")
        encoded = _encode_json(dict(payload), "payload")
        tries = _max_tries(max_tries)
        sid = _text(session_id, "session_id", MAX_ID_LEN) if session_id is not None else None
        rid = _text(run_id, "run_id", MAX_ID_LEN) if run_id is not None else secrets.token_urlsafe(RUN_ID_BYTES)

        with self._tx(tenant) as conn:
            if sid is not None:
                known = fetch_one(
                    conn,
                    "SELECT id FROM sessions WHERE id = %s AND tenant_id = %s",
                    (sid, tenant),
                )
                if known is None:
                    raise RunsValidationError("unknown_session_id")
            # ON CONFLICT 而不是先 SELECT：跨租户同 id 时 RLS 看不见对方行，
            # 先查会漏判、再插入又变成主键异常（500）。DO NOTHING 让两种情况都回到 400。
            row = fetch_one(
                conn,
                "INSERT INTO runs (id, tenant_id, session_id, status, kind, payload, max_tries)"
                " VALUES (%s, %s, %s, 'queued', %s, %s::jsonb, %s)"
                " ON CONFLICT (id) DO NOTHING"
                f" RETURNING {_RUN_COLUMNS}",
                (rid, tenant, sid, kind_text, encoded, tries),
            )
        if row is None:
            raise RunsValidationError("run_id_already_exists")
        return row

    def claim(self, *, lease_seconds: int = 300) -> dict[str, Any] | None:
        """领取一个 queued run（单语句 + SKIP LOCKED）；无任务返回 None。"""
        lease = _lease_seconds(lease_seconds)
        row: dict[str, Any] | None = None
        with self._worker_tx() as conn:
            row = fetch_one(conn, _CLAIM_SQL, (lease,))
            if row is not None:
                # 同一事务内立刻切到该行租户，并清掉 worker GUC：此后（含本事务剩余部分）
                # 一切读写都走普通租户 RLS，跨租户可见性只活到这一句。
                execute(conn, "SELECT set_config('app.tenant_id', %s, true)", (row["tenant_id"],))
                execute(conn, "SELECT set_config('app.tenant', %s, true)", (row["tenant_id"],))
                execute(conn, "SELECT set_config('app.current_tenant', %s, true)", (row["tenant_id"],))
                execute(conn, "SELECT set_config('app.queue_worker', '', true)")
        if row is None:
            return None
        self._remember_claim(str(row["id"]), str(row["tenant_id"]), int(row["attempt"]))
        return row

    def heartbeat(self, run_id: str, attempt: int, *, lease_seconds: int = 300) -> bool:
        """续租；被 fencing（0 行）返回 False 并打 runs.write_fenced。"""
        rid = _text(run_id, "run_id", MAX_ID_LEN)
        att = _attempt(attempt)
        lease = _lease_seconds(lease_seconds)
        tenant = self._claim_tenant(rid, "heartbeat", att)
        if tenant is None:
            return False
        with self._tx(tenant) as conn:
            row = fetch_one(conn, _HEARTBEAT_SQL, (lease, rid, att))
        if row is None:
            self._log_fenced("heartbeat", rid, att)
            return False
        return True

    def finish(self, run_id: str, attempt: int, *, result: Any = None) -> bool:
        """成功收尾（fencing）；0 行 = 丢弃写并返回 False。"""
        rid = _text(run_id, "run_id", MAX_ID_LEN)
        att = _attempt(attempt)
        encoded = _encode_json(result, "result")
        tenant = self._claim_tenant(rid, "finish", att)
        if tenant is None:
            self._forget(rid, att)
            return False
        with self._tx(tenant) as conn:
            row = fetch_one(conn, _FINISH_SQL, (encoded, rid, att))
        if row is None:
            self._log_fenced("finish", rid, att)
            self._forget(rid, att)
            return False
        self._forget(rid, att)
        return True

    def fail(self, run_id: str, attempt: int, *, error: str) -> dict[str, Any]:
        """失败收尾（fencing）：attempt >= max_tries → dead，否则回 queued。"""
        rid = _text(run_id, "run_id", MAX_ID_LEN)
        att = _attempt(attempt)
        message = _clip_error(error)
        tenant = self._claim_tenant(rid, "fail", att)
        if tenant is None:
            self._forget(rid, att)
            return {"id": rid, "status": "fenced", "requeued": False, "dead": False, "attempt": att}
        with self._tx(tenant) as conn:
            row = fetch_one(conn, _FAIL_SQL, (message, rid, att))
        self._forget(rid, att)
        if row is None:
            self._log_fenced("fail", rid, att)
            return {"id": rid, "status": "fenced", "requeued": False, "dead": False, "attempt": att}
        status = str(row["status"])
        return {
            "id": rid,
            "status": status,
            "requeued": status == "queued",
            "dead": status == "dead",
            "attempt": int(row["attempt"]),
            "max_tries": int(row["max_tries"]),
        }

    # ------------------------------------------------------------------ 取消
    def request_cancel(self, tenant_id: str, run_id: str) -> dict[str, Any]:
        """queued 直接 cancelled；running 置 cancelling（worker 在 step 边界转 cancelled）。"""
        tenant = _text(tenant_id, "tenant_id", MAX_TENANT_LEN)
        rid = _text(run_id, "run_id", MAX_ID_LEN)
        with self._tx(tenant) as conn:
            row = fetch_one(conn, _CANCEL_SQL, (tenant, rid))
            if row is None:
                existing = fetch_one(
                    conn,
                    f"SELECT {_RUN_COLUMNS} FROM runs WHERE tenant_id = %s AND id = %s",
                    (tenant, rid),
                )
                if existing is None:
                    raise RunNotFound("unknown run")
                logger.info(
                    "runs.cancel_ignored run_id=%s status=%s", rid, existing.get("status")
                )
                return {**existing, "cancelled": False}
        return {**row, "cancelled": True}

    def settle_cancel(self, tenant_id: str, run_id: str, attempt: int) -> bool:
        """把 cancelling 转成 cancelled（step 边界调用）；被 fencing 时返回 False。"""
        tenant = _text(tenant_id, "tenant_id", MAX_TENANT_LEN)
        rid = _text(run_id, "run_id", MAX_ID_LEN)
        att = _attempt(attempt)
        with self._tx(tenant) as conn:
            row = fetch_one(conn, _SETTLE_CANCEL_SQL, (rid, att))
        if row is None:
            logger.info("runs.cancel_not_settled run_id=%s attempt=%s", rid, att)
            return False
        return True

    # ---------------------------------------------------------------- 看门狗
    def reap_expired(self) -> dict[str, int]:
        """回收过期租约：超次置 dead，其余回 queued；返回 {requeued, dead}。"""
        with self._worker_tx() as conn:
            dead = fetch_all(conn, _REAP_DEAD_SQL)
            requeued = fetch_all(conn, _REAP_REQUEUE_SQL)
        if dead or requeued:
            logger.info("runs.reaped dead=%d requeued=%d", len(dead), len(requeued))
        return {"requeued": len(requeued), "dead": len(dead)}

    # ------------------------------------------------------------------ 读取
    def get(self, tenant_id: str, run_id: str) -> dict[str, Any]:
        tenant = _text(tenant_id, "tenant_id", MAX_TENANT_LEN)
        rid = _text(run_id, "run_id", MAX_ID_LEN)
        with self._tx(tenant) as conn:
            row = fetch_one(
                conn,
                f"SELECT {_RUN_COLUMNS} FROM runs WHERE tenant_id = %s AND id = %s",
                (tenant, rid),
            )
        if row is None:
            raise RunNotFound("unknown run")
        return row

    def list(
        self, tenant_id: str, *, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        tenant = _text(tenant_id, "tenant_id", MAX_TENANT_LEN)
        capped = _limit(limit)
        status_text: str | None = None
        if status is not None:
            status_text = _text(status, "status", MAX_KIND_LEN)
            if status_text not in RUN_STATUSES:
                raise RunsValidationError("status_not_supported")
        with self._tx(tenant) as conn:
            if status_text is None:
                return fetch_all(
                    conn,
                    f"SELECT {_RUN_COLUMNS} FROM runs WHERE tenant_id = %s"
                    " ORDER BY created_at DESC, id DESC LIMIT %s",
                    (tenant, capped),
                )
            return fetch_all(
                conn,
                f"SELECT {_RUN_COLUMNS} FROM runs WHERE tenant_id = %s AND status = %s"
                " ORDER BY created_at DESC, id DESC LIMIT %s",
                (tenant, status_text, capped),
            )

    def _status(self, tenant_id: str, run_id: str) -> str:
        """轻量状态读（取消边界检查用）；行不存在返回空串。"""
        tenant = _text(tenant_id, "tenant_id", MAX_TENANT_LEN)
        rid = _text(run_id, "run_id", MAX_ID_LEN)
        with self._tx(tenant) as conn:
            row = fetch_one(
                conn, "SELECT status FROM runs WHERE tenant_id = %s AND id = %s", (tenant, rid)
            )
        return str(row["status"]) if row else ""

    def _log_fenced(self, op: str, run_id: str, attempt: int) -> None:
        """fencing 丢弃写：这是安全不变量生效（不是错误），但要留痕可观测。"""
        logger.warning(
            "runs.write_fenced run_id=%s attempt=%s op=%s error_code=runs.write_fenced",
            run_id,
            attempt,
            op,
        )


__all__ = [
    "CANCELLABLE_STATUSES",
    "KINDS",
    "MAX_LIMIT",
    "RUN_STATUSES",
    "RunNotFound",
    "RunQueue",
    "RunsError",
    "RunsUnavailable",
    "RunsValidationError",
    "TERMINAL_STATUSES",
]
