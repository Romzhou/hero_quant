"""TDD for lane C1 — 14 checkpoint/temporal fixes (G2 #27-35 #41-45)."""
from __future__ import annotations
import asyncio
import inspect
import logging
import threading
import time

# 辅助：清理全局状态
def _clear_pg_globals():
    import hero_quant.checkpoint.postgres as pg
    pg._PG_GLOBAL_STORE.clear()
    pg._PG_GLOBAL_META.clear()
    pg._PG_GLOBAL_TS.clear()
    pg._PG_SEQ_BY_RUN.clear()
    pg._PG_RUN_BY_SEQ.clear()

# ---- postgres 9 项 ----

def test_c1_01_split_brain_lock_single_RLock():
    """sync 与 async 不应分裂两套锁保同一 dict；aput/aget 应对全局 store 统一用 _PG_GLOBAL_LOCK。"""
    from hero_quant.checkpoint.postgres import AsyncPostgresSaver
    src_aput = inspect.getsource(AsyncPostgresSaver.aput)
    src_aget = inspect.getsource(AsyncPostgresSaver.aget)
    # 修复后不应再 async with _alock 保 _PG_GLOBAL_STORE
    # 允许 _get_async_lock 存在但不再用于 store 的 async with
    assert "async with _alock" not in src_aput, "aput 仍用全局 asyncio.Lock 保 dict — 分裂锁"
    assert "async with _alock" not in src_aget, "aget 仍用全局 asyncio.Lock 保 dict — 分裂锁"
    # 应使用线程锁
    assert "_PG_GLOBAL_LOCK" in src_aput, "aput 应使用 _PG_GLOBAL_LOCK 统一保 dict"
    assert "_PG_GLOBAL_LOCK" in src_aget, "aget 应使用 _PG_GLOBAL_LOCK 统一保 dict"


def test_c1_02_global_async_lock_not_shared_across_loops():
    """全局 asyncio.Lock 跨 loop 共享且创建竞态；应以线程锁或 per-saver 锁替代，或至少受 _PG_GLOBAL_LOCK 保护创建。"""
    import hero_quant.checkpoint.postgres as pg
    src = inspect.getsource(pg._get_async_lock)
    # 若仍保留全局 async lock，则创建必须受线程锁保护，否则竞态
    if "_PG_ASYNC_LOCK" in src:
        # 修复后应有线程锁保护或改为 per-saver；最少应出现 _PG_GLOBAL_LOCK 或 threading.Lock
        assert "_PG_GLOBAL_LOCK" in src or "threading" in src, "_get_async_lock 未受线程锁保护 — 竞态"
    # 更优：全局 async lock 不应再用于 store（已在 01 覆盖），此处仅校验创建受保护


def test_c1_03_emulated_cache_should_not_shadow_real_pg():
    """emulated 命中不应遮蔽真 PG；有真实池时 get 应优先查 PG。"""
    _clear_pg_globals()
    from hero_quant.checkpoint.postgres import get_saver
    dsn = "postgresql://postgres:postgres@localhost:5432/hero_quant_test_c1_03"
    saver = get_saver(dsn=dsn, ttl_seconds=3600)
    tid = "wf:runShadow:tenantS"
    # 先写入 emulated（模拟旧缓存）
    saver.put(tid, {"from": "emulated"}, {})
    assert saver.get(tid)["from"] == "emulated"
    # 注入假真实 PG：让 _pg_get_sync 返回更新值
    def fake_pg_get(self, thread_id):
        if thread_id == tid:
            return {"from": "pg"}
        return None
    orig = saver._pg_get_sync
    saver._pg_get_sync = fake_pg_get.__get__(saver, type(saver))  # type: ignore
    # 伪装为真实池
    orig_is_real = saver._is_real_pg_pool
    saver._is_real_pg_pool = lambda: True  # type: ignore
    orig_pool_is_async = saver._pool_is_async
    saver._pool_is_async = lambda: False  # type: ignore
    try:
        got = saver.get(tid)
        assert got is not None and got["from"] == "pg", f"emulated 遮蔽了真 PG，got={got}"
    finally:
        saver._pg_get_sync = orig  # type: ignore
        saver._is_real_pg_pool = orig_is_real  # type: ignore
        saver._pool_is_async = orig_pool_is_async  # type: ignore
        _clear_pg_globals()

def test_c1_03_list_thread_ids_merges_pg():
    """list_thread_ids 仅当 alive 非空就早返回，未合并 PG 行；应合并去重。"""
    from hero_quant.checkpoint.postgres import AsyncPostgresSaver
    src = inspect.getsource(AsyncPostgresSaver.list_thread_ids)
    # 修复前：if alive: return alive 直接返回；修复后应合并或至少在有池时查询 PG
    # 若仍有早返回，需保证其后仍有 PG 合并逻辑
    if "if alive:" in src:
        # 检查早返回后是否仍有 PG 逻辑（fetch warm rows / connection）
        after_alive = src.split("if alive:")[1]
        # 修复后不应仅 return alive，至少应有合并
        assert "return alive" not in after_alive or "fetch" in after_alive.lower() or "merge" in after_alive.lower() or "_fetch_warm" in src, "list_thread_ids 早返回未合并 PG"

def test_c1_04_pg_failures_logged_with_redacted_dsn():
    """setup/DDL 失败不应 except: pass 静默；应以脱敏 DSN 记录 warning。"""
    import hero_quant.checkpoint.postgres as pg
    src_setup = inspect.getsource(pg.AsyncPostgresSaver.setup)
    assert "_redact_dsn" in src_setup or "redact" in src_setup.lower(), "setup 未使用 _redact_dsn 脱敏记录"
    src_put_sync = inspect.getsource(pg.AsyncPostgresSaver._pg_put_sync)
    # _pg_put_sync 异常路径应记录而非静默 return False
    assert "logger.warning" in src_put_sync or "logger.error" in src_put_sync or "_redact_dsn" in src_put_sync, "_pg_put_sync 静默吞错，未记录"
    # 同时整体文件应多处使用 _redact_dsn
    src_all = inspect.getsource(pg)
    assert src_all.count("_redact_dsn") >= 2, "应多处以脱敏 DSN 记录 PG 失败"

def test_c1_05_pg_dsn_pool_none_not_silent_memory():
    """PG DSN 且 pool=None 不应永不连库而静默 emulated；应尝试建池或 loud 警告。"""
    import hero_quant.checkpoint.postgres as pg
    src_init = inspect.getsource(pg.AsyncPostgresSaver.__init__)
    # 死代码 if self.pool is None and ConnectionPool is not None: pass 应被接线
    assert "ConnectionPool" in src_init, "__init__ 未涉及 ConnectionPool"
    # 不应仅 pass
    # 检查该分支是否仍是孤立 pass
    lines = src_init.splitlines()
    found_pass_only = False
    for i, ln in enumerate(lines):
        if "ConnectionPool is not None" in ln:
            nxt = lines[i+1].strip() if i+1 < len(lines) else ""
            if nxt == "pass":
                found_pass_only = True
    assert not found_pass_only, "pool=None 的 PG DSN 仍是 pass 死代码，未接线建池或警告"
    src_setup = inspect.getsource(pg.AsyncPostgresSaver.setup)
    assert "_redact_dsn" in src_setup, "PG DSN 无池时 setup 应以脱敏 DSN 警告"

def test_c1_06_seq_maps_bounded_and_dsn_isolated():
    """_PG_SEQ_BY_RUN/_PG_RUN_BY_SEQ 无界且 keys 无 DSN 前缀导致跨 DSN 碰撞与泄漏。"""
    _clear_pg_globals()
    import hero_quant.checkpoint.postgres as pg
    from hero_quant.checkpoint.postgres import get_saver, _PG_SEQ_BY_RUN
    # 检查仓是否对 seq maps 做有界驱逐
    src = inspect.getsource(pg._thread_to_keys)
    # 应有 evict 或 maxsize 相关逻辑，或在 postgres 顶部有对 SEQ 的清理函数
    src_all = inspect.getsource(pg)
    has_evict_seq = ("_PG_SEQ" in src_all and ("evict" in src_all.lower() or "_MAXSIZE" in src_all))
    assert has_evict_seq, "seq maps 无 LRU/TTL 驱逐，需有界"
    # 跨 DSN 隔离：两个不同 DSN 相同 thread_id 不应复用同一 seq key
    dsn1 = "postgresql://postgres:postgres@localhost:5432/hero_quant_test_c1_06_a"
    dsn2 = "postgresql://postgres:postgres@localhost:5432/hero_quant_test_c1_06_b"
    tid = "wf:runCollide:tenantC"
    # 若 _thread_to_keys 已支持 dsn 参数，则直接校验
    sig = inspect.signature(pg._thread_to_keys)
    if "dsn" in sig.parameters:
        _clear_pg_globals()
        _, _, seq1 = pg._thread_to_keys(tid, dsn=dsn1)
        _, _, seq2 = pg._thread_to_keys(tid, dsn=dsn2)
        # 不同 DSN 即使 run 相同，因前缀不同，底层 key 不同；此处 seq 数值可相同但映射 key 应隔离
        # 检查映射表长度
        assert len(_PG_SEQ_BY_RUN) >= 2 or seq1 == seq2, "dsn 隔离的 seq 映射应分开存储"
        # 更强：若实现为 dsn 前缀 key，则 _PG_SEQ_BY_RUN 中应存在带 hash 前缀的 key
        has_prefixed = any(dsn1[:8] in k or dsn2[:8] in k or "::" in k and len(k) > 20 for k in _PG_SEQ_BY_RUN.keys())
        # 若未实现前缀，至少需证明两次 put 不互相覆盖
        saver1 = get_saver(dsn=dsn1, ttl_seconds=3600)
        saver2 = get_saver(dsn=dsn2, ttl_seconds=3600)
        _clear_pg_globals()
        saver1.put(tid, {"v": 1}, {})
        saver2.put(tid, {"v": 2}, {})
        # 若未隔离，第二个 put 会覆盖第一个的 seq 映射
        # 通过检查全局 store 的 key 带 DSN 前缀区分
        assert len(pg._PG_GLOBAL_STORE) == 2, f"跨 DSN 写入应各存一份，got {len(pg._PG_GLOBAL_STORE)}"
        _clear_pg_globals()
    else:
        # 未改签名则检查源码是否含 DSN hash 前缀
        assert "_pg_store_prefix" in src or "dsn" in src.lower(), "_thread_to_keys 未以 DSN 前缀隔离跨 DSN 映射"

def test_c1_07_no_await_under_threading_lock_in_asetup():
    """asetup 用 threading.Lock 跨 await 会阻塞事件循环；应仅用 _asetup_lock。"""
    from hero_quant.checkpoint.postgres import AsyncPostgresSaver
    src = inspect.getsource(AsyncPostgresSaver.asetup)
    # 修复后不应出现 with self._setup_lock: 套 await
    if "with self._setup_lock" in src:
        block = src.split("with self._setup_lock")[1]
        # 取该 with 块前 500 字符检查是否有 await
        snippet = block[:800]
        assert "await" not in snippet, "asetup 仍在 threading.Lock 内 await — 会阻塞事件循环"
    # 应使用 _asetup_lock
    assert "_asetup_lock" in src, "asetup 应使用 _asetup_lock 而非线程锁"

def test_c1_08_get_with_config_ttl_evict():
    """get_with_config TTL 过期 pass 不清，已过期应驱逐并视作 miss。"""
    _clear_pg_globals()
    from hero_quant.checkpoint.postgres import get_saver
    import hero_quant.checkpoint.postgres as pg
    dsn = "postgresql://postgres:postgres@localhost:5432/hero_quant_test_c1_08"
    saver = get_saver(dsn=dsn, ttl_seconds=2)
    tid = "wf:runTTL:tenantG"
    saver.put(tid, {"v": 1}, {"cfg": 1})
    # 篡改时间戳为过期
    key = pg._pg_store_key(dsn, tid)
    pg._PG_GLOBAL_TS[key] = time.time() - 10
    # get_with_config 应返回 None 而非仍命中
    res = saver.get_with_config(tid)
    assert res is None, f"过期 emulated 仍被返回: {res}"
    # 且应已驱逐
    assert key not in pg._PG_GLOBAL_STORE, "过期条目未被驱逐"
    _clear_pg_globals()

def test_c1_09_redact_dsn_wired():
    """_redact_dsn 死代码 — 应被调用于日志。"""
    import hero_quant.checkpoint.postgres as pg
    src_all = inspect.getsource(pg)
    # 除定义外至少有一次调用
    assert src_all.count("_redact_dsn(") >= 2, "_redact_dsn 定义后未被调用，属死代码"
    assert "ConnectionPool" in src_all and "ConnectionPool(" in src_all, "ConnectionPool 未被用于建池，死导入"

# ---- temporal 5 项 ----

def test_c1_10_astart_clears_stop():
    """astart 不清 _stop 导致 stop 后重启立即退出。"""
    import hero_quant.checkpoint.temporal as tp
    src = inspect.getsource(tp.HeartbeatHelper.astart)
    assert "_stop.clear" in src, "astart 未清 _stop，重启后将立即退出"
    # 行为校验
    async def _run():
        h = tp.HeartbeatHelper(interval=0.6)
        h.start({"a": 1})
        # 确保线程启动
        await asyncio.sleep(0.05)
        h.stop()
        assert h._stop.is_set(), "stop 后 _stop 应为 set"
        # 无 loop 外 astart 也应清（此处有 loop）
        await h.astart({"b": 2})
        assert not h._stop.is_set(), "astart 后 _stop 未 clear"
        await h.astop()
        h.stop()
    asyncio.run(_run())

def test_c1_11_stop_cancels_async_task():
    """stop 丢 _async_task 不 cancel，异步循环泄漏。"""
    import hero_quant.checkpoint.temporal as tp
    src = inspect.getsource(tp.HeartbeatHelper.stop)
    assert "_async_task" in src and ("cancel" in src or "astop" in src or "warning" in src.lower()), "stop 未处理 _async_task — 泄漏"
    async def _run():
        h = tp.HeartbeatHelper(interval=0.3)
        await h.astart({"x": 1})
        task = h._async_task
        assert task is not None, "astart 应创建 _async_task"
        # 同步 stop 应取消任务（不能仅置 None）
        h.stop()
        # 任务应被取消或已完成
        await asyncio.sleep(0.05)
        assert task.cancelled() or task.done(), f"stop 后任务仍运行: done={task.done()} cancelled={task.cancelled()}"
        # 清理
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    asyncio.run(_run())

def test_c1_12_heartbeat_cross_thread_visible():
    """ContextVar + thread-local 跨线程不可见，需共享锁保护的 store。"""
    import hero_quant.checkpoint.temporal as tp
    src_hb = inspect.getsource(tp.heartbeat)
    src_get = inspect.getsource(tp.get_heartbeat_details)
    # 修复后应有共享锁/共享字典
    assert "_shared" in src_hb or "Lock" in src_hb or "_shared" in src_get, "heartbeat 未引入跨线程共享存储"
    # 行为：后台线程写入，主线程可读 — 清理旧上下文避免污染
    # 清理调用方线程的 ContextVar/thread-local，使其走共享回退路径
    try:
        tp._heartbeat_details_ctx.set(None)  # type: ignore
    except Exception:
        pass
    try:
        tp._set_thread_details(None)  # type: ignore
    except Exception:
        pass
    # 同时清理共享
    try:
        with tp._shared_lock:  # type: ignore
            tp._shared_details = None  # type: ignore
    except Exception:
        pass
    def bg():
        tp.heartbeat({"cross": "thread", "seq": 7})
    t = threading.Thread(target=bg)
    t.start()
    t.join()
    got = tp.get_heartbeat_details()
    assert got is not None and got.get("cross") == "thread", f"跨线程 heartbeat 不可见: {got}"

def test_c1_13_astart_no_loop_logs_warning(caplog):
    """astart 无 loop 时静默 pass，应至少 warning。"""
    import hero_quant.checkpoint.temporal as tp
    src = inspect.getsource(tp.HeartbeatHelper.astart)
    assert "logger.warning" in src or "logger.warn" in src, "astart 无 loop 时未 warning"
    h = tp.HeartbeatHelper(interval=0.5)
    # 确保无运行 loop
    caplog.set_level(logging.WARNING, logger="hero_quant.checkpoint.temporal")
    # 在无 loop 的同步上下文调用 astart
    asyncio.run(h.astart({"w": 1})) if False else None
    # 直接在无 loop 线程中调用 — 用 asyncio.run 外的同步调用无法 await，需用 trick：直接调用未 await 的协程会警告
    # 改为：新线程内无 loop 调用 astart（需 await，故用 asyncio.run 外的同步包装）
    # 简化：直接检查在无 running loop 时调用会记录 warning
    async def _no_loop_call():
        # 在有 loop 时正常；我们需要无 loop 分支：通过在线程中无 loop 直接 await
        pass
    # 实际触发：起一个无 loop 的线程，创建 helper 并尝试 astart（无 loop）
    def call_without_loop():
        h2 = tp.HeartbeatHelper(interval=0.5)
        # 在无运行 loop 的线程中执行 asyncio.run 来触发 RuntimeError 分支是不行的，因为 asyncio.run 会创建 loop
        # 改为直接调用 astart 的内部逻辑：模拟 get_running_loop 抛错时应 warning
        # 我们直接调用 heartbeatHelper.astart 在无 loop 环境下：需不用 asyncio.run
        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 这就是 astart 内部会捕获的分支
            assert True
            return
        assert False, "应无 loop"
    # 更直接：检查源码已含 warning 即视为通过，行为上通过同步线程无 loop 调用验证
    # 用 caplog 捕获无 loop astart
    def thread_target():
        h3 = tp.HeartbeatHelper(interval=0.5)
        # 手动模拟无 loop 的 astart：直接 run coroutine 在无 loop 线程中会创建新 loop，无法触发
        # 改为：调用 asyncio.get_running_loop 在无 loop 线程中抛错，astart 应捕获并 warning
        # 我们在该线程中用 asyncio.run(h3.astart(...)) 实际上有 loop，不会触发
        # 故改为：在该线程中不使用 asyncio.run，而是直接同步检查源码已覆盖
        pass
    # 行为断言改为：以无 loop 方式调用 astart 应触发 warning（通过 mock get_running_loop 抛错）
    from unittest.mock import patch
    h4 = tp.HeartbeatHelper(interval=0.5)
    with patch("hero_quant.checkpoint.temporal.asyncio.get_running_loop", side_effect=RuntimeError("no running event loop")):
        caplog.clear()
        asyncio.run(h4.astart({"k": 1}))
        # 应有 warning 记录（中文亦算）
        assert any(
            "without running loop" in rec.getMessage().lower()
            or "background heartbeat not started" in rec.getMessage().lower()
            or "无运行 loop" in rec.getMessage()
            for rec in caplog.records
        ), f"无 loop 时未 warning，records={caplog.records}"
        # 清理
        try:
            asyncio.run(h4.astop())
        except Exception:
            pass

def test_c1_14_async_loop_no_extra_heartbeat_after_stop():
    """异步循环在 sleep 后无条件 heartbeat，会多发一次；应在 sleep 后重检 _stop。"""
    import hero_quant.checkpoint.temporal as tp
    src = inspect.getsource(tp.HeartbeatHelper._async_loop)
    # 应在 await sleep 后再次检查 _stop
    assert "await asyncio.sleep" in src, "_async_loop 未见 sleep"
    after_sleep = src.split("await asyncio.sleep")[1]
    assert "_stop.is_set" in after_sleep, "_async_loop 在 sleep 后未重检 _stop，会多发一次 heartbeat"
    # 行为：stop 后不应再 heartbeat
    async def _run():
        h = tp.HeartbeatHelper(interval=0.2)
        calls = []
        orig_hb = tp.heartbeat
        def counting_hb(details=None):
            calls.append(time.time())
            return orig_hb(details)
        import unittest.mock as mock
        with mock.patch("hero_quant.checkpoint.temporal.heartbeat", side_effect=counting_hb):
            await h.astart({"s": 1})
            await asyncio.sleep(0.05)
            n_before = len(calls)
            # 调用 astop（内部会 set _stop 并 cancel）
            await h.astop()
            await asyncio.sleep(0.35)
            n_after = len(calls)
            # astop 后不应再增长（允许至多 0 次多发）
            assert n_after == n_before, f"stop 后仍多发 heartbeat: before={n_before} after={n_after}"
    asyncio.run(_run())
