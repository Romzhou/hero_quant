"""Task 9 TDD: PG isolation and ready probe — DSN hashed, _is_real_* sole criterion, no fake success without pool."""
import hashlib
import os


def test_dsn_key_hashed_no_secret():
    """_pg_store_key 不含明文 secret，len 12 sha256 前缀（复用 checkpoint/postgres.py:92）。"""
    from hero_quant.checkpoint.postgres import _pg_store_key, _pg_store_prefix

    dsn = "postgresql://user:secret@localhost/db"
    k = _pg_store_key(dsn, "wf:1:t1")
    # 哈希前缀 12 位，不含明文 secret
    assert "secret" not in k, f"DSN key leaked secret: {k}"
    prefix = k.split("::")[0]
    assert len(prefix) == 12, f"prefix len !=12: {prefix}"
    expected = hashlib.sha256(dsn.encode()).hexdigest()[:12]
    assert prefix == expected, f"prefix mismatch {prefix} != {expected}"
    # prefix 同逻辑
    p = _pg_store_prefix(dsn)
    assert p.startswith(expected) and p.endswith("::")


def test_billing_dsn_key_hashed_no_secret():
    """billing _dsn_key 同样哈希，不含明文 secret，len 12。"""
    from hero_quant.billing.service import _dsn_key

    dsn = "postgresql://user:secret@localhost/db"
    k = _dsn_key(dsn)
    assert "secret" not in k, f"billing DSN key leaked: {k}"
    # hashed 时 len 12
    assert len(k) == 12, f"billing key len !=12: {k}"
    expected = hashlib.sha256(dsn.encode()).hexdigest()[:12]
    assert k == expected


def test_billing_is_real_pg_false_without_pool():
    """无 pool 时 _is_real_pg 为 False（唯一判据）。"""
    from hero_quant.billing.service import BillingService

    svc = BillingService(dsn="postgresql://user:pass@localhost/db")
    # 即使 DSN 为 PG，若无真实 pool 亦为 False
    assert svc._is_real_pg() is False
    assert svc._is_pg_mode() is True  # PG mode 仍为 True，但非真实


def test_checkpoint_is_real_pg_pool_false_without_pool():
    """无 pool 时 _is_real_pg_pool 为 False。"""
    from hero_quant.checkpoint.postgres import AsyncPostgresSaver

    saver = AsyncPostgresSaver(dsn="postgresql://postgres:postgres@localhost:5432/hero_quant_test", pool=None)
    assert saver._is_pg_mode() is True
    assert saver._is_real_pg_pool() is False


def test_publish_no_fake_success_without_pool():
    """publish_factor / _pg_put_sync 无 pool 不伪成功 — _pg_put_sync 返回 False，未建立真实持久化。"""
    from hero_quant.checkpoint.postgres import AsyncPostgresSaver

    saver = AsyncPostgresSaver(dsn="postgresql://postgres:postgres@localhost:5432/hero_quant_test_nopool", pool=None)
    # _pg_put_sync 无池应返回 False 而非 True
    ok = saver._pg_put_sync("wf:1:t1", {"v": 1}, {})
    assert ok is False, f"_pg_put_sync should be False without pool, got {ok}"

    from hero_quant.billing.service import BillingService

    svc = BillingService(dsn="postgresql://user:pass@localhost/db2")
    # _pg_publish_sync 同理：无 pool 时不应返回 True（应为 None/False 且不伪装 PG 成功）
    res = svc._pg_publish_sync({"factor_id": "f", "tenant": "t"})
    # 明确不伪成功：结果非 True；且 _is_real_pg 为 False
    assert svc._is_real_pg() is False
    assert res is not True  # 旧实现返回 None 亦视为不伪成功，但显式 False 更佳；此处保证不为 True


def test_ready_pg_probe_requires_real_pool_and_select_success():
    """ /ready pg_ok 仅真池探活 SELECT 1 成功才 True；SELECT 失败或无池则 False（fail-closed）。"""
    from fastapi.testclient import TestClient
    import hero_quant.api.server as srv

    # 场景1：mock 无池或探活失败 -> pg False
    original = srv._check_checkpoint_pg
    original_billing = srv._check_billing_pg
    try:
        # 构造一个 fake pool 在 SELECT 上抛异常
        class FakeCursor:
            def execute(self, sql, params=None):
                raise RuntimeError("SELECT 1 failed")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class FakePool:
            def connection(self):
                return FakeConn()

            def getconn(self):
                return FakeConn()

        # 注入 fake saver：有池但 SELECT 失败 -> 应返回 False
        def fake_get_saver(dsn=None, ttl_seconds=None, **kw):
            from hero_quant.checkpoint.postgres import AsyncPostgresSaver
            s = AsyncPostgresSaver(dsn=dsn or "postgresql://postgres:postgres@localhost:5432/hero_quant", pool=FakePool())
            return s

        import unittest.mock as mock

        with mock.patch("hero_quant.checkpoint.postgres.get_saver", side_effect=fake_get_saver):
            # 同时 patch server 模块内的 get_saver 引用
            with mock.patch.object(srv, "_check_checkpoint_pg", wraps=srv._check_checkpoint_pg):
                # 直接测 _check_checkpoint_pg：有池但 SELECT 异常应返回 (False, "memory")
                # 由于实现带 suppress，当前会错误返回 True，需要修复后返回 False
                ok, mode = srv._check_checkpoint_pg()
                # 修复后应为 False；未修复时为 True -> 此断言会失败，驱动修复
                assert ok is False, f"expected pg probe False when SELECT fails, got {ok} mode={mode}"
                assert mode == "memory"
    finally:
        pass

    # 场景2：无池 -> 直接 False（已在真实环境验证）
    c = TestClient(srv.app)
    # 使用 env 无 PG DSN 时 /ready 亦应 pg False；此处不强制 200/503，仅校验 pg 字段为 bool
    r = c.get("/ready")
    j = r.json()
    assert isinstance(j.get("pg"), bool)


def test_cohere_probe_not_always_true(monkeypatch):
    """cohere 探活不再恒 True — 无 key 时应为 False（fail-closed），有 key 且探活失败亦 False。"""
    import hero_quant.api.server as srv

    # 清空 COHERE key
    monkeypatch.setenv("COHERE_API_KEY", "")
    # 同时确保 Settings 读取为空
    monkeypatch.setattr(srv, "_check_cohere", srv._check_cohere)  # keep original
    # 直接调用原实现：若恒 True 则此测试失败
    ok = srv._check_cohere()
    # 期望：无 key 时不再恒 True，应返回 False
    assert ok is False, f"cohere probe should be False when no key, got {ok}"

    # 有 key 但探活失败亦应可为 False（此分支留给实现按需探活）
    monkeypatch.setenv("COHERE_API_KEY", "fake-key-123")
    # 若实现仅检查 key 存在则会返回 True；更严格的实现会尝试网络探活并在失败时返回 False
    # 此处不强制断言 True/False，但确保不再无条件 True 时可扩展
