"""K线 G-15 回归锁：已到位的纵深逐项锁定（肯定项防退化）。

中文：G-15 所列 6 项 defensive-in-depth 全部有约可测；本文件只锁行为/源码存在性，
不碰 src（B 线 tests 独占但 K 线新增单测文件不冲突；若 B 线同期改同名文件，以 B 为准，
本文件可整体改名避让）。
"""
from __future__ import annotations

import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_k_g15_ticket_atomic_single_use():
    from hero_quant.api import security as sec

    src = (_REPO_ROOT / "src/hero_quant/api/security.py").read_text(encoding="utf-8")
    assert "getdel" in src and "eval" in src, "票据原子路径（GETDEL+Lua 兜底）不得退化"
    t = sec.issue_ticket(ttl=60)
    assert sec.consume_ticket(t) is True
    assert sec.consume_ticket(t) is False, "票据必须单次有效"


def test_k_g15_ticket_scope_isolation_runtime():
    from hero_quant.api import security as sec

    q = sec.issue_ticket(ttl=60, scope="query")
    assert sec.consume_ticket_with_user(q, require_scope="metrics") == (False, None)
    q2 = sec.issue_ticket(ttl=60, scope="query")
    ok, _ = sec.consume_ticket_with_user(q2, require_scope="query")
    assert ok is True
    legacy = sec.issue_ticket(ttl=60)
    ok_legacy, _ = sec.consume_ticket_with_user(legacy, require_scope="metrics")
    assert ok_legacy is True, "legacy 无分域票迁移期须放行"


def test_k_g15_host_fail_closed():
    from hero_quant.api.security import check_host

    assert check_host("", ["example.com"]) is False
    assert check_host("example.com", []) is False
    assert check_host("example.com", ["example.com"]) is True


def test_k_g15_constant_time_compare_present():
    src = (_REPO_ROOT / "src/hero_quant/api/security.py").read_text(encoding="utf-8")
    assert src.count("compare_digest") >= 2, "HMAC/API-Key 须常量时间比较"


def test_k_g15_billing_rls_ddl_present():
    svc = (_REPO_ROOT / "src/hero_quant/billing/service.py").read_text(encoding="utf-8")
    assert "FORCE ROW LEVEL SECURITY" in svc
    assert "purchases_insert" in svc
    mig = (_REPO_ROOT / "migrations/002_billing_rls.sql").read_text(encoding="utf-8")
    assert "FORCE" in mig and "purchases_insert" in mig, "migration 只读核对一致"


def test_k_g15_dsn_key_hashed_and_redacted():
    from hero_quant.billing.service import _dsn_key

    k = _dsn_key("postgresql://alice:s3cret@db:5432/app")
    assert "s3cret" not in k, "DSN 口令不得驻留 dict key"


def test_k_g15_redaction_unknown_sink_strictest():
    from hero_quant.security import redaction as rd

    assert isinstance(getattr(rd, "ARGUMENTS_SINK", None), str) and getattr(rd, "ARGUMENTS_SINK")
    out = rd.redact_payload({"api_key": "sk-live-1234567890"}, sink="__unknown_sink__")
    assert "sk-live-1234567890" not in str(out), "未知 sink 须回退最严"
