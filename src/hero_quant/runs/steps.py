"""runs.steps — run_steps 幂等断点台账（PR-202）。

契约（docs/contracts/batch-14.md §2.2）：
- 主键 `(run_id, step_name)`；写入一律 `INSERT ... ON CONFLICT DO NOTHING`；
- 恢复时先查已完成 step 直接跳过（`done_steps` / `_done_outputs`，只认 succeeded）；
- `status` 允许 succeeded / failed，但只有 succeeded 算断点：failed 行是诊断记录，不会被跳过
  （该 step 会在下一次 attempt 重跑）。worker 因此只写 succeeded —— 若写 failed 行，重跑成功后
  受 DO NOTHING 限制无法改写，会出现「run 成功、step 仍 failed」的假象。

`done_steps` 的签名不含 tenant_id，所以 StepLedger 需要一个固定的租户上下文
（worker 用 run.tenant_id 构造）。没有租户上下文时直接报错，绝不退化成跨租户读。
"""

from __future__ import annotations

from typing import Any

from hero_quant.db.rows import fetch_all, fetch_one
from hero_quant.runs.store import (
    MAX_ERROR_LEN,
    MAX_ID_LEN,
    MAX_TENANT_LEN,
    RunsValidationError,
    _PoolBacked,
    _attempt,
    _encode_json,
    _text,
)

STEP_COLUMNS = "step_name, status, attempt, output, error, created_at"
STEP_STATUSES = frozenset({"succeeded", "failed"})


class StepLedger(_PoolBacked):
    """run_steps 读写方；构造需注入连接池（测试可传假 pool）。"""

    def __init__(self, pool: Any, *, tenant_id: str | None = None) -> None:
        super().__init__(pool)
        tenant = tenant_id.strip() if isinstance(tenant_id, str) and tenant_id.strip() else None
        self._tenant = tenant[:MAX_TENANT_LEN] if tenant else None

    @property
    def tenant_id(self) -> str | None:
        return self._tenant

    def _scoped_tenant(self, tenant_id: str | None = None) -> str:
        candidate = tenant_id if tenant_id is not None else self._tenant
        if candidate is None:
            raise RunsValidationError("tenant_required")
        return _text(candidate, "tenant_id", MAX_TENANT_LEN)

    def done_steps(self, run_id: str) -> set[str]:
        """已成功的 step 名集合（恢复时据此跳过）。"""
        rid = _text(run_id, "run_id", MAX_ID_LEN)
        tenant = self._scoped_tenant()
        with self._tx(tenant) as conn:
            rows = fetch_all(
                conn,
                "SELECT step_name FROM run_steps WHERE run_id = %s AND status = 'succeeded'",
                (rid,),
            )
        return {str(row["step_name"]) for row in rows}

    def record(
        self,
        run_id: str,
        tenant_id: str,
        step_name: str,
        *,
        status: str = "succeeded",
        output: Any = None,
        attempt: int = 0,
        error: str | None = None,
    ) -> bool:
        """写一条 step；重复 step 返回 False（ON CONFLICT DO NOTHING）。"""
        rid = _text(run_id, "run_id", MAX_ID_LEN)
        tenant = _text(tenant_id, "tenant_id", MAX_TENANT_LEN)
        name = _text(step_name, "step_name", MAX_ID_LEN)
        status_text = _text(status, "status", 16)
        if status_text not in STEP_STATUSES:
            raise RunsValidationError("step_status_not_supported")
        att = _attempt(attempt)
        encoded = _encode_json(output, "output")
        error_text = None if error is None else str(error)[:MAX_ERROR_LEN]
        with self._tx(tenant) as conn:
            row = fetch_one(
                conn,
                "INSERT INTO run_steps"
                " (run_id, tenant_id, step_name, status, attempt, output, error)"
                " VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)"
                " ON CONFLICT (run_id, step_name) DO NOTHING"
                " RETURNING step_name",
                (rid, tenant, name, status_text, att, encoded, error_text),
            )
        return row is not None

    def _done_outputs(self, run_id: str) -> dict[str, Any]:
        """已成功 step → output 映射（断点恢复把旧结果交回 handler）。"""
        rid = _text(run_id, "run_id", MAX_ID_LEN)
        tenant = self._scoped_tenant()
        with self._tx(tenant) as conn:
            rows = fetch_all(
                conn,
                "SELECT step_name, output FROM run_steps"
                " WHERE run_id = %s AND status = 'succeeded'",
                (rid,),
            )
        return {str(row["step_name"]): row.get("output") for row in rows}

    def _list_steps(self, run_id: str) -> list[dict[str, Any]]:
        """该 run 的全部 step 行（时间正序）；供 API/SSE 计算进度。"""
        rid = _text(run_id, "run_id", MAX_ID_LEN)
        tenant = self._scoped_tenant()
        with self._tx(tenant) as conn:
            return fetch_all(
                conn,
                f"SELECT {STEP_COLUMNS} FROM run_steps WHERE run_id = %s"
                " ORDER BY created_at, step_name",
                (rid,),
            )


__all__ = ["STEP_STATUSES", "StepLedger"]
