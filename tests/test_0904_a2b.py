"""Lane A2b TDD — 幂等与账本隔离 11 条，每条一个 failing test（先红后绿）。

覆盖 scan_0904_g2_state.log 中 governance 两文件 11 节：
dedup 5 条（wait_for_async 堵 loop、PG jsonb 纯 str、RLS/DDL 静默、BEGIN 重试丢原子性、PG mark 无锁读 _mem），
ledger 6 条（增量 verify 缓存命中跳全扫 critical、rotate 独占锁 fail-open、rotate TOCTOU、租户内 index 当全局、entries.index O(n2)、compute_record_hash 死分支）。
契约：fail-closed；租户隔离失败 loud；原子性；中文注释；窄化捕获 + logger.warning(exc_info=True)。
只动 src/hero_quant/governance/dedup.py 与 ledger.py。
"""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest


# ── dedup 5 条 ────────────────────────────────────────────────────


def test_b01_wait_for_async_offloads_sync_get():
    """wait_for_async 调同步 get 堵 loop：必须经 asyncio.to_thread 卸载，不可直调阻塞 IO。"""
    from hero_quant.governance.dedup import DedupStore

    src = inspect.getsource(DedupStore.wait_for_async)
    assert "to_thread" in src


def test_b02_pg_mark_encodes_plain_str_for_jsonb():
    """纯 str 结果破 PG jsonb cast：'ok' 必须编码为 '"ok"' 再 ::jsonb。"""
    from hero_quant.governance.dedup import DedupStore

    captured: dict = {}

    class _FakeConn:
        def execute(self, sql, params=None):
            captured.setdefault("calls", []).append((sql, params))

            class _C:
                rowcount = 1

            return _C()

        def cursor(self):
            raise AssertionError("不应走到 cursor 回退")

        def commit(self):
            pass

    class _FakeCM:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            return self._conn

        def __exit__(self, *a):
            return False

    class _FakePool:
        def __init__(self, conn):
            self._conn = conn

        def connection(self):
            return _FakeCM(self._conn)

    store = DedupStore("memory://a2b-b02")
    store._is_pg = True
    store.pool = _FakePool(_FakeConn())
    assert store._pg_mark_sync("t1:k1", "SUCCESS", result="ok") is True
    update_params = [p for s, p in captured["calls"] if "UPDATE dedup" in s][0]
    assert update_params[1] == '"ok"'


def test_b03_rls_ddl_failure_is_loud():
    """RLS/DDL 失败静默（租户隔离失效）：执行失败必须抛，不吞。"""
    from hero_quant.governance.dedup import DedupStore

    class _BoomConn:
        def execute(self, *a, **k):
            raise OSError("mock RLS boom")

        def cursor(self):
            class _CM:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def execute(self, *a, **k):
                    raise OSError("mock RLS boom")

            return _CM()

        def commit(self):
            pass

    class _FakeCM:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            return self._conn

        def __exit__(self, *a):
            return False

    class _FakePool:
        def __init__(self, conn):
            self._conn = conn

        def connection(self):
            return _FakeCM(self._conn)

    store = DedupStore("memory://a2b-b03")
    store._is_pg = True
    store.pool = _FakePool(_BoomConn())
    with pytest.raises(Exception):
        store._pg_setup_sync()


def test_b04_begin_retry_failure_not_nonatomic():
    """BEGIN IMMEDIATE 重试失败丢原子性：不得无事务继续，必须抛。"""
    import sqlite3

    from hero_quant.governance.dedup import DedupStore

    store = DedupStore("memory://a2b-b04")
    store.db_path = Path(str(store.db_path)) if store.db_path else None
    # 用真实临时库 + 必败的 BEGIN 包装，迫使走重试失败分支
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    store2 = DedupStore(tmp.name)
    real_connect = store2._connect

    class _BoomConn:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a, **k):
            if "BEGIN IMMEDIATE" in sql:
                raise sqlite3.OperationalError("mock locked")
            return self._real.execute(sql, *a, **k)

        def close(self):
            return self._real.close()

    store2._connect = lambda: _BoomConn(real_connect())  # type: ignore
    try:
        with pytest.raises(Exception):
            store2.insert_pending("t1:b04", "tool")
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def test_b05_pg_mark_mem_read_under_lock():
    """PG mark 路径无锁读 _mem：必须在 self._lock 下读取。"""
    from hero_quant.governance.dedup import DedupStore

    src = inspect.getsource(DedupStore._pg_mark_sync)
    assert "with self._lock" in src


# ── ledger 6 条 ───────────────────────────────────────────────────


def test_b06_append_never_skips_full_verify_on_cache_hit(tmp_path):
    """critical：增量 verify 缓存命中跳全扫——中间同长篡改 + 伪造 mtime 后 append 必须抛。"""
    from hero_quant.governance.ledger import Ledger, LedgerCorruptionError, _tail_verify_cache

    p = tmp_path / "b06.jsonl"
    led = Ledger(p)
    led.append({"k": "v0001"}, tenant="t1")
    led.append({"k": "v0002"}, tenant="t1")
    led.append({"k": "v0003"}, tenant="t1")
    assert str(p) in _tail_verify_cache
    st = p.stat()
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    # 中文：同长替换中间行 payload（v0002→w0002），长度/size 不变；二进制写避免换行符转换改变 size
    assert "v0002" in lines[1]
    lines[1] = lines[1].replace("v0002", "w0002")
    p.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    assert p.stat().st_size == st.st_size, "用例要求同长篡改（size 不变）"
    # 中文：伪造 mtime 回原值，凑齐四元组（count/mtime/size/tail）命中条件
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    with pytest.raises(LedgerCorruptionError):
        led.append({"k": "v0004"}, tenant="t1")


def test_b07_rotate_exclusive_lock_fail_closed(tmp_path, monkeypatch):
    """rotate 吞独占锁失败 fail-open：锁失败必须抛 LedgerCorruptionError，不裸奔。"""
    from hero_quant.governance import ledger as lm

    p = tmp_path / "b07.jsonl"
    led = lm.Ledger(p)
    led.append({"k": "v"}, tenant="t1")

    def _boom(handle):
        raise OSError("mock lock boom")

    monkeypatch.setattr(lm, "_lock_exclusive", _boom)
    with pytest.raises(lm.LedgerCorruptionError):
        lm.rotate_if_needed(p, max_bytes=1)


def test_b08_rotate_verify_inside_exclusive_lock():
    """rotate 预检后释放锁再 verify（TOCTOU）：空临界区必须消除，verify 收拢进排他锁后、rename 前。"""
    from hero_quant.governance.ledger import rotate_if_needed

    src = inspect.getsource(rotate_if_needed)
    assert "try:\n                    pass\n                finally:" not in src
    # 中文：按行定位——真实加锁行、verify 行、rename 调用行，顺序须为 锁 < verify < rename
    lines = src.splitlines()
    lock_ln = next(i for i, ln in enumerate(lines) if "_lock_exclusive(_locked_h)" in ln)
    # 中文：verify 经复用锁句柄读 + _verify_entries 内联（Windows 强制锁下另开句柄会被拒）
    verify_ln = next(i for i, ln in enumerate(lines) if "_rot_ok, _rot_brk = tmp._verify_entries" in ln)
    rename_ln = next(i for i, ln in enumerate(lines) if "path.rename(archive)" in ln)
    assert lock_ln < verify_ln < rename_ln


def test_b09_tenant_break_index_is_global(tmp_path):
    """租户内 index 当全局 break 上报：多租户断裂必须报全局下标。"""
    from hero_quant.governance.ledger import Ledger

    p = tmp_path / "b09.jsonl"
    led = Ledger(p)
    led.append({"k": "a1"}, tenant="a")
    led.append({"k": "b1"}, tenant="b")
    led.append({"k": "a2"}, tenant="a")
    led.append({"k": "b2"}, tenant="b")
    entries = led._read_all()
    assert [e["seq"] for e in entries] == [1, 2, 3, 4]
    # 中文：篡改全局第 4 条（tenant b 第二条）payload，不断裂 seq，只断裂 hash
    entries[3] = dict(entries[3])
    entries[3]["record"] = {"k": "evil"}
    ok, brk = led._verify_entries(entries)
    assert ok is False
    assert brk is not None
    assert brk.index == 3


def test_b10_no_list_index_in_verify_paths():
    """entries.index(e) O(n2)+重复行错位：两处 verify 路径一律改 enumerate。"""
    from hero_quant.governance import ledger as lm

    assert "entries.index(e)" not in inspect.getsource(lm.Ledger._verify_entries)
    # 中文：全局下标经 enumerate/pos_by_id 推导，不用 list.index（O(n²)+重复行错位）
    assert "enumerate(entries)" in inspect.getsource(lm.Ledger._verify_entries)
    assert "records.index(r)" not in inspect.getsource(lm.verify_export)


def test_b11_compute_record_hash_no_dead_branch():
    """compute_record_hash 死分支：两臂相同，必须坍缩为单次 envelope 调用。"""
    from hero_quant.governance.ledger import compute_record_hash

    src = inspect.getsource(compute_record_hash)
    assert "if price is not None" not in src
    assert "_tenant_payload_hash" in src
    assert compute_record_hash(1, "sha256:genesis", {"k": "v"}).startswith("sha256:")
    _ = json.dumps({"ok": True})
