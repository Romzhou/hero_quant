"""批次14 契约：队列/审计链的表结构由 0005/0006 迁移锁定。

这些断言只校验"DDL 文本包含约定要素"，真 DB 行为由对应 PG 族用例覆盖
（tests/test_pr201_queue_pg.py 等）。目的是防止后续有人改迁移时把队列语义改掉
（状态枚举、fencing 用的 attempt、run_steps 幂等主键、审计链头表）。
"""

from __future__ import annotations

from hero_quant.db.loader import load_migrations
from hero_quant.db.runner import EXPECTED_TABLES


def _sql(version: int) -> str:
    for m in load_migrations():
        if m.version == version:
            return "\n".join(m.statements)
    raise AssertionError(f"migration {version} missing")


def test_runs_queue_columns_present():
    sql = _sql(5)
    for column in ("kind", "attempt", "max_tries", "lease_until", "heartbeat_at", "payload", "result", "error"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql, column
    # session_id 必须可空：长任务不挂在会话下
    assert "ALTER COLUMN session_id DROP NOT NULL" in sql


def test_runs_status_enum_covers_queue_states():
    sql = _sql(5)
    for state in ("queued", "running", "succeeded", "cancelled", "cancelling", "dead"):
        assert f"'{state}'" in sql, state


def test_run_steps_is_idempotent_breakpoint_table():
    sql = _sql(5)
    assert "CREATE TABLE IF NOT EXISTS run_steps" in sql
    assert "PRIMARY KEY (run_id, step_name)" in sql
    assert "REFERENCES runs(id) ON DELETE CASCADE" in sql


def test_queue_claim_and_lease_indexes():
    sql = _sql(5)
    assert "idx_runs_claim" in sql and "WHERE status = 'queued'" in sql
    assert "idx_runs_lease" in sql and "WHERE status = 'running'" in sql


def test_audit_chain_columns_and_head_table():
    sql = _sql(6)
    assert "ADD COLUMN IF NOT EXISTS prev_hash" in sql
    assert "ADD COLUMN IF NOT EXISTS entry_hash" in sql
    assert "CREATE TABLE IF NOT EXISTS audit_chain_head" in sql


def test_expected_tables_include_batch14_tables():
    assert "run_steps" in EXPECTED_TABLES
    assert "audit_chain_head" in EXPECTED_TABLES
