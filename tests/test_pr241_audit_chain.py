"""PR-241：链式哈希（entry_hash / AuditChain.append / verify）离线契约用例。

不碰真 PG：store 用替身（只提供 _tx），SQL 的读写入口用 monkeypatch 顶替，
锁行为/回滚/RLS 由 tests/test_pr241_audit_pg.py 在真库上验证。

这里锁死三件事：
1. entry_hash 的规范化口径（字段顺序、canonical JSON、ts 归一到 UTC、id 不参与）；
2. 追加时 prev_hash/entry_hash 与链头 UPSERT 的一致性（同事务、由 prev 串起来）；
3. verify 用返回值表达断点（hash_mismatch / prev_mismatch / head_mismatch），
   并如实给出 chain_start_entry_id / chain_start_at / uncovered_count。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from hero_quant.audit import chain as chain_mod
from hero_quant.audit.chain import GENESIS_HASH, AuditChain, entry_hash
from hero_quant.audit.store import AuditValidationError

_TENANT = "tenant-a"
_T0 = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
_AUTO = object()


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------
class _FakeStore:
    """只提供 _tx：真实现里 set_config + conn.transaction 由 store 负责，链层不该重建连接。"""

    def __init__(self) -> None:
        self.tx_calls: list[str] = []

    @contextmanager
    def _tx(self, tenant):
        self.tx_calls.append(tenant)
        yield object()


def _row(index: int, *, action="query.completed", detail=None) -> dict:
    return {
        "id": index,
        "ts": _T0 + timedelta(seconds=index),
        "tenant_id": _TENANT,
        "action": action,
        "resource": None,
        "run_id": None,
        "model": None,
        "prompt_hash": None,
        "result_hash": None,
        "ip": None,
        "detail": {} if detail is None else detail,
    }


def _chain_rows(count: int, **kwargs) -> list[dict]:
    """按库里真实形态造行：prev/entry 串好，ts 读回后是 ISO 串（jsonable 口径）。"""
    prev = GENESIS_HASH
    rows: list[dict] = []
    for index in range(1, count + 1):
        row = _row(index, **kwargs)
        row["prev_hash"] = prev
        row["entry_hash"] = entry_hash(prev, row)
        prev = row["entry_hash"]
        rows.append({**row, "ts": row["ts"].isoformat()})
    return rows


class _VerifyDb:
    def __init__(self, rows, *, uncovered=0, head=_AUTO) -> None:
        self.rows = list(rows)
        self.uncovered = uncovered
        if head is _AUTO:
            self.head = (
                {
                    "head_id": self.rows[-1]["id"],
                    "head_hash": self.rows[-1]["entry_hash"],
                    "entries": len(self.rows),
                    "updated_at": "2026-10-01T00:00:00.000000+00:00",
                }
                if self.rows
                else None
            )
        else:
            self.head = head
        self.sqls: list[str] = []

    def fetch_one(self, conn, sql, params=None):
        self.sqls.append(sql)
        if "FOR UPDATE" in sql:
            raise AssertionError("verify 不该加锁（只读校验）")
        if "uncovered_count" in sql:  # 必须先于 covered_count：前者是后者的子串
            return {"uncovered_count": self.uncovered}
        if "covered_count" in sql:
            return {"covered_count": len(self.rows)}
        if "ORDER BY id ASC LIMIT 1" in sql:
            if not self.rows:
                return None
            return {"id": self.rows[0]["id"], "ts": self.rows[0]["ts"]}
        if "audit_chain_head" in sql:
            return self.head
        raise AssertionError(f"unexpected sql: {sql}")

    def fetch_all(self, conn, sql, params=None):
        self.sqls.append(sql)
        assert "audit_log" in sql and params is not None
        return self.rows[: int(params[1])]


def _install_verify(monkeypatch, db: _VerifyDb) -> _FakeStore:
    store = _FakeStore()
    monkeypatch.setattr(chain_mod, "fetch_one", db.fetch_one)
    monkeypatch.setattr(chain_mod, "fetch_all", db.fetch_all)
    return store


class _AppendDb:
    """记录 append 走过的事务内语句与参数（模拟 RETURNING）。"""

    def __init__(self, head: dict | None = None) -> None:
        self.head = head
        self.locked = 0
        self.append_params = None
        self.upsert_params = None
        self.advisory_locks: list[str] = []
        self.sqls: list[str] = []

    def execute(self, conn, sql, params=None):
        self.sqls.append(sql)
        if "pg_advisory_xact_lock" in sql:
            self.advisory_locks.append(params[0])
        return None

    def fetch_one(self, conn, sql, params=None):
        self.sqls.append(sql)
        if "FOR UPDATE" in sql:
            self.locked += 1
            return self.head
        if "INSERT INTO audit_log" in sql:
            self.append_params = params
            return {
                "id": 42,
                "ts": params[0].isoformat(),
                "tenant_id": params[1],
                "user_id": params[2],
                "action": params[3],
                "resource": params[4],
                "run_id": params[5],
                "model": params[6],
                "prompt_hash": params[7],
                "result_hash": params[8],
                "ip": params[9],
                "detail": {},
                "prev_hash": params[11],
                "entry_hash": params[12],
            }
        if "audit_chain_head" in sql and "INSERT" in sql:
            self.upsert_params = params
            return {"head_id": params[1], "head_hash": params[2], "entries": params[3]}
        raise AssertionError(f"unexpected sql: {sql}")


def _install_append(monkeypatch, db: _AppendDb) -> _FakeStore:
    store = _FakeStore()
    monkeypatch.setattr(chain_mod, "fetch_one", db.fetch_one)
    monkeypatch.setattr(chain_mod, "execute", db.execute)
    return store


# ---------------------------------------------------------------------------
# entry_hash 口径
# ---------------------------------------------------------------------------
def test_genesis_hash_is_64_zeros():
    assert GENESIS_HASH == "0" * 64
    assert len(GENESIS_HASH) == 64


def test_entry_hash_is_stable_and_ignores_id():
    row = _row(7)
    first = entry_hash(GENESIS_HASH, row)
    assert entry_hash(GENESIS_HASH, row) == first, "同一行两次调用必须同值"
    assert entry_hash(GENESIS_HASH, {**row, "id": 999}) == first, "自增 id 不参与规范化"


def test_entry_hash_changes_with_every_hashed_field():
    base = _row(1)
    baseline = entry_hash(GENESIS_HASH, base)
    for field, value in (
        ("tenant_id", "tenant-b"),
        ("action", "query.failed"),
        ("resource", "res-1"),
        ("run_id", "run-1"),
        ("model", "m-1"),
        ("prompt_hash", "p" * 32),
        ("result_hash", "r" * 32),
        ("ip", "203.0.113.9"),
    ):
        assert entry_hash(GENESIS_HASH, {**base, field: value}) != baseline, field


def test_entry_hash_detail_is_canonical_json():
    left = _row(1, detail={"b": 2, "a": {"y": 1, "x": [1, 2]}})
    right = _row(1, detail={"a": {"x": [1, 2], "y": 1}, "b": 2})
    assert entry_hash(GENESIS_HASH, left) == entry_hash(GENESIS_HASH, right)
    assert entry_hash(GENESIS_HASH, left) != entry_hash(GENESIS_HASH, _row(1, detail={"a": 1}))


def test_entry_hash_detail_none_matches_stored_empty_object():
    assert entry_hash(GENESIS_HASH, _row(1, detail=None)) == entry_hash(
        GENESIS_HASH, _row(1, detail={})
    )


def test_entry_hash_ts_normalizes_to_same_instant():
    """写端 datetime、读端 timestamptz->ISO 串；同一瞬间必须同 hash（跨时区）。"""
    utc_row = _row(1)
    same_instant = {
        **utc_row,
        # 同一瞬间的 +08:00 表示（astimezone 只换时区，不换瞬间）
        "ts": utc_row["ts"].astimezone(timezone(timedelta(hours=8))).isoformat(),
    }
    assert entry_hash(GENESIS_HASH, same_instant) == entry_hash(GENESIS_HASH, utc_row)
    other_instant = {**utc_row, "ts": (utc_row["ts"] + timedelta(seconds=1)).isoformat()}
    assert entry_hash(GENESIS_HASH, other_instant) != entry_hash(GENESIS_HASH, utc_row)


def test_entry_hash_prev_hash_participates_and_is_validated():
    row = _row(1)
    assert entry_hash(GENESIS_HASH, row) != entry_hash("f" * 64, row)
    with pytest.raises(ValueError):
        entry_hash("", row)
    with pytest.raises(ValueError):
        entry_hash(None, row)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# append：事务内锁链头 -> 算 hash -> INSERT -> UPSERT 链头
# ---------------------------------------------------------------------------
def test_append_uses_genesis_when_no_head(monkeypatch):
    db = _AppendDb(head=None)
    store = _install_append(monkeypatch, db)
    moment = _T0

    row = AuditChain(store).append(
        _TENANT, "tool.call", resource="quote", run_id="run-1", detail={"ok": True}, ts=moment
    )

    assert row["prev_hash"] == GENESIS_HASH
    params = db.append_params
    expected = entry_hash(
        GENESIS_HASH,
        {
            "ts": moment,
            "tenant_id": _TENANT,
            "action": "tool.call",
            "resource": "quote",
            "run_id": "run-1",
            "model": None,
            "prompt_hash": None,
            "result_hash": None,
            "ip": None,
            "detail": {"ok": True},
        },
    )
    assert params[12] == expected == row["entry_hash"]
    assert db.upsert_params == (_TENANT, 42, expected, 1)
    assert any("FOR UPDATE" in sql for sql in db.sqls), "追加必须先锁链头"
    assert any("ON CONFLICT" in sql and "audit_chain_head" in sql for sql in db.sqls)
    assert store.tx_calls == [_TENANT]


def test_append_links_to_existing_head_and_bumps_entries(monkeypatch):
    head = {"head_id": 41, "head_hash": "a" * 64, "entries": 5}
    db = _AppendDb(head=head)
    store = _install_append(monkeypatch, db)

    row = AuditChain(store).append(_TENANT, "backtest.run", ts=_T0)

    assert row["prev_hash"] == "a" * 64
    assert db.append_params[12] == entry_hash("a" * 64, {**_row(1), "action": "backtest.run", "ts": _T0})
    assert db.upsert_params == (_TENANT, 42, db.append_params[12], 6)
    assert db.advisory_locks == [], "已有链头时不该再抢 bootstrap 锁"


def test_append_bootstraps_with_advisory_lock_only_when_head_missing(monkeypatch):
    db = _AppendDb(head=None)
    store = _install_append(monkeypatch, db)

    AuditChain(store).append(_TENANT, "session.run", ts=_T0)

    assert db.advisory_locks == [f"hero_quant:audit_chain:{_TENANT}"]
    assert db.locked == 2, "锁不到行 -> 抢 bootstrap 锁 -> 复核链头"


def test_append_rejects_unknown_fields_before_touching_store(monkeypatch):
    db = _AppendDb(head=None)
    store = _install_append(monkeypatch, db)
    with pytest.raises(AuditValidationError):
        AuditChain(store).append(_TENANT, "tool.call", ts=_T0, bogus=1)
    assert db.sqls == [] and store.tx_calls == []


def test_append_rejects_sensitive_detail(monkeypatch):
    db = _AppendDb(head=None)
    store = _install_append(monkeypatch, db)
    with pytest.raises(AuditValidationError):
        AuditChain(store).append(_TENANT, "tool.call", ts=_T0, detail={"prompt": "原文"})
    assert db.sqls == [], "敏感 detail 在取连接前就该拒绝"


# ---------------------------------------------------------------------------
# verify：返回值表达断点
# ---------------------------------------------------------------------------
def test_verify_ok_reports_coverage(monkeypatch):
    rows = _chain_rows(3)
    db = _VerifyDb(rows, uncovered=2)
    store = _install_verify(monkeypatch, db)

    report = AuditChain(store).verify(_TENANT)

    assert report["ok"] is True
    assert report["entries"] == 3
    assert report["head_hash"] == rows[-1]["entry_hash"]
    assert report["broken_at"] is None and report["reason"] is None
    assert report["degraded"] is False and report["reason_code"] is None
    assert report["chain_start_entry_id"] == 1
    assert report["chain_start_at"] == rows[0]["ts"]
    assert report["uncovered_count"] == 2


def test_verify_detects_content_tampering(monkeypatch):
    rows = _chain_rows(3)
    rows[1] = {**rows[1], "detail": {"tampered": True}}  # hash 未同步更新
    db = _VerifyDb(rows)
    store = _install_verify(monkeypatch, db)

    report = AuditChain(store).verify(_TENANT)

    assert report["ok"] is False
    assert report["broken_at"] == rows[1]["id"]
    assert report["reason"] == "hash_mismatch"
    assert report["entries"] == 2, "断点行计入已扫描数，但不再往下校验"


def test_verify_detects_missing_middle_row(monkeypatch):
    rows = _chain_rows(3)
    del rows[1]  # 中间行被删：下一行的 prev_hash 指不回上一行
    db = _VerifyDb(rows)
    store = _install_verify(monkeypatch, db)

    report = AuditChain(store).verify(_TENANT)

    assert report["ok"] is False
    assert report["broken_at"] == 3
    assert report["reason"] == "prev_mismatch"


def test_verify_detects_tail_truncation_via_head(monkeypatch):
    rows = _chain_rows(3)
    rows = rows[:2]  # 尾部行被删，链环本身没断
    db = _VerifyDb(rows, head={"head_id": 2, "head_hash": "b" * 64, "entries": 3})
    store = _install_verify(monkeypatch, db)

    report = AuditChain(store).verify(_TENANT)

    assert report["ok"] is False
    assert report["reason"] == "head_mismatch"
    assert report["broken_at"] is None


def test_verify_marks_limit_truncation_degraded(monkeypatch):
    rows = _chain_rows(5)
    db = _VerifyDb(rows)
    store = _install_verify(monkeypatch, db)

    report = AuditChain(store).verify(_TENANT, limit=2)

    assert report["entries"] == 2
    assert report["ok"] is False, "只校验前缀时不得声称整链完整"
    assert report["degraded"] is True
    assert report["reason_code"] == "scan_truncated"


def test_verify_reports_missing_head_as_degraded(monkeypatch):
    rows = _chain_rows(2)
    db = _VerifyDb(rows, head=None)
    store = _install_verify(monkeypatch, db)

    report = AuditChain(store).verify(_TENANT)

    assert report["ok"] is False
    assert report["degraded"] is True
    assert report["reason_code"] == "head_missing"
    assert report["head_hash"] == rows[-1]["entry_hash"]


def test_verify_empty_chain_is_not_a_fake_success(monkeypatch):
    db = _VerifyDb([], uncovered=4, head=None)
    store = _install_verify(monkeypatch, db)

    report = AuditChain(store).verify(_TENANT)

    assert report["ok"] is True and report["entries"] == 0
    assert report["head_hash"] is None, "无入链行时不许拿 GENESIS 冒充链头"
    assert report["chain_start_entry_id"] is None and report["chain_start_at"] is None
    assert report["uncovered_count"] == 4


def test_verify_rejects_bool_limit(monkeypatch):
    db = _VerifyDb(_chain_rows(1))
    store = _install_verify(monkeypatch, db)
    with pytest.raises(AuditValidationError):
        AuditChain(store).verify(_TENANT, limit=True)
