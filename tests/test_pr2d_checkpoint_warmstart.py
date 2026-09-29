"""PR2-D TDD: checkpoint warm-start + run_text 持久化。

范围：只碰 checkpoint/postgres.py 与 server.py lifespan。
- 构造含 run_text 的 checkpoints 行，重启（清 _PG_SEQ_BY_RUN 后调 warm 函数）
  后 list_thread_ids 映射非空且 run_text 可查；
- 真 PG 缺席时回退内存 str(seq) 不抛。
"""
from __future__ import annotations


def _make_fake_pool(dsn, rows):
    class FakeConn:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, sql, params=None):
            rows = self._rows

            class Cur:
                def fetchall(self):
                    return rows

                def fetchone(self):
                    return rows[0] if rows else None

            return Cur()

        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakePool:
        def __init__(self, rows):
            self._rows = rows
            self.conninfo = dsn

        def connection(self):
            return FakeConn(self._rows)

    return FakePool(rows)


def _clear_dsn_globals(pg, dsn):
    prefix = f"{__import__('hashlib').sha256(dsn.encode()).hexdigest()[:12]}::"
    for k in list(pg._PG_GLOBAL_STORE.keys()):
        if k.startswith(prefix):
            pg._PG_GLOBAL_STORE.pop(k, None)
            pg._PG_GLOBAL_TS.pop(k, None)
            pg._PG_GLOBAL_META.pop(k, None)


def test_warm_restores_run_mapping_after_restart(monkeypatch):
    """含 run_text 的行 -> 清映射 -> warm 后 list_thread_ids 恢复原始 run 且 run_text 可查。"""
    import hero_quant.checkpoint.postgres as pg
    from hero_quant.checkpoint.postgres import _thread_to_keys, get_saver

    # 中文：禁用真实池创建（CI 有可达 PG service 时会误建真池），保证本测恒走注入的 fake pool
    monkeypatch.setattr(pg, "ConnectionPool", None)

    dsn = "postgresql://postgres:postgres@localhost:5432/hero_quant_test_pr2d_warm"
    _clear_dsn_globals(pg, dsn)
    pg._PG_SEQ_BY_RUN.clear()
    pg._PG_RUN_BY_SEQ.clear()

    tid = "wf:myrun-PR2D:tenantW"
    tenant, thread, seq = _thread_to_keys(tid)
    assert hasattr(pg, "warm_checkpoint_maps"), "missing warm_checkpoint_maps (TDD red)"
    saver = get_saver(dsn=dsn, ttl_seconds=3600)
    # fake 真 PG：返回含 run_text 的行
    saver.pool = _make_fake_pool(dsn, [(tenant, thread, seq, "myrun-PR2D")])
    # 清 emulated alive 与实例缓存，强制走真 PG 路径
    _clear_dsn_globals(pg, dsn)
    saver._timestamps.clear()
    saver._store.clear()

    # 模拟重启：清内存映射后调 warm 函数
    pg._PG_SEQ_BY_RUN.clear()
    pg._PG_RUN_BY_SEQ.clear()
    warmed = pg.warm_checkpoint_maps(saver)
    assert warmed >= 1, f"warm should restore >=1 mapping, got {warmed}"

    ids = saver.list_thread_ids()
    assert tid in ids, f"warm-start lost original run, got {ids}"
    # run_text 可查
    assert hasattr(pg, "get_run_text") or hasattr(saver, "get_run_text")
    if hasattr(pg, "get_run_text"):
        assert pg.get_run_text(tenant, thread, seq) == "myrun-PR2D"
    else:
        assert saver.get_run_text(tenant, thread, seq) == "myrun-PR2D"
    # 清理
    _clear_dsn_globals(pg, dsn)
    pg._PG_SEQ_BY_RUN.clear()
    pg._PG_RUN_BY_SEQ.clear()


def test_no_real_pg_falls_back_to_memory_str_seq(monkeypatch):
    """真 PG 缺席（pool=None）时 list_thread_ids 不抛；真 PG 路径无映射回退内存 str(seq)。"""
    import hero_quant.checkpoint.postgres as pg
    from hero_quant.checkpoint.postgres import _thread_to_keys, get_saver

    # 中文：禁用真实池创建——CI 中 psycopg_pool 已安装且 PG service 可达，不隔离会误建真池
    monkeypatch.setattr(pg, "ConnectionPool", None)

    dsn = "postgresql://postgres:postgres@localhost:5432/hero_quant_test_pr2d_nopg"
    _clear_dsn_globals(pg, dsn)
    pg._PG_SEQ_BY_RUN.clear()
    pg._PG_RUN_BY_SEQ.clear()
    saver = get_saver(dsn=dsn, ttl_seconds=3600)
    assert saver.pool is None  # 真 PG 缺席
    # warm 无池不抛，返回 0
    assert hasattr(pg, "warm_checkpoint_maps")
    assert pg.warm_checkpoint_maps(saver) == 0

    # 真 PG 路径但无映射：回退 str(seq) 不抛
    dsn2 = "postgresql://postgres:postgres@localhost:5432/hero_quant_test_pr2d_fallback"
    _clear_dsn_globals(pg, dsn2)
    pg._PG_SEQ_BY_RUN.clear()
    pg._PG_RUN_BY_SEQ.clear()
    tid = "wf:plainrun:tenantF"
    tenant, thread, seq = _thread_to_keys(tid)
    pg._PG_SEQ_BY_RUN.clear()
    pg._PG_RUN_BY_SEQ.clear()  # 删映射模拟旧数据
    saver2 = get_saver(dsn=dsn2, ttl_seconds=3600)
    saver2.pool = _make_fake_pool(dsn2, [(tenant, thread, seq)])  # 无 run_text 列
    _clear_dsn_globals(pg, dsn2)
    saver2._timestamps.clear()
    saver2._store.clear()
    ids = saver2.list_thread_ids()  # 不抛
    assert f"{thread}:{seq}:{tenant}" in ids
    _clear_dsn_globals(pg, dsn2)
    pg._PG_SEQ_BY_RUN.clear()
    pg._PG_RUN_BY_SEQ.clear()
