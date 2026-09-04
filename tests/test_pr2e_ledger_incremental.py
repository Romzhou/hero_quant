"""PR2-E: ledger O(n) 增量校验 — TDD.

范围：只碰 governance/ledger.py（保留 _tail_verify_cache，在其上加批量增量校验）。
- 缓存命中（含 tail hash + count/mtime/size 一致）跳过全扫；
- 未命中但前缀可信时仅校验新增段；
- 篡改尾部后增量校验能检出；
- 批量 append 后 tail hash 链连续。
"""
import json
import os
import time

import pytest


def test_cache_hit_skips_full_scan(tmp_path, monkeypatch):
    """稳态追加命中 tail 缓存时必须跳过 O(n) 全扫（CI 放宽断言：缓存命中跳过全扫即可）。"""
    from hero_quant.governance.ledger import Ledger

    p = tmp_path / "ledger.jsonl"
    lg = Ledger(p)
    for i in range(3):
        lg.append({"i": i})

    calls = {"n": 0}
    orig = Ledger._verify_entries

    def counting(self, entries):
        calls["n"] += 1
        return orig(self, entries)

    monkeypatch.setattr(Ledger, "_verify_entries", counting)
    lg.append({"i": 3})
    lg.append({"i": 4})
    assert calls["n"] == 0, "cache hit 必须跳过全扫 _verify_entries"
    assert lg.verify() is True


def test_tail_tamper_detected_on_next_append(tmp_path):
    """篡改尾部 payload 后，下一次 append 的增量校验必须检出并拒绝扩展。"""
    from hero_quant.governance.ledger import Ledger, LedgerCorruptionError

    p = tmp_path / "ledger.jsonl"
    lg = Ledger(p)
    for i in range(5):
        lg.append({"v": i})
    lines = p.read_text(encoding="utf-8").splitlines()
    obj = json.loads(lines[-1])
    obj["record"]["v"] = 9999
    lines[-1] = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert lg.verify() is False
    with pytest.raises(LedgerCorruptionError):
        lg.append({"v": "after-tamper"})


def test_tail_tamper_same_size_with_forged_mtime_detected(tmp_path):
    """同长篡改尾部 + 伪造 mtime 回到缓存值时，O(1) 尾记录复核仍须检出。"""
    from hero_quant.governance import ledger as mod
    from hero_quant.governance.ledger import Ledger, LedgerCorruptionError

    p = tmp_path / "ledger.jsonl"
    lg = Ledger(p)
    lg.append({"v": 10000})
    lg.append({"v": 20000})
    cached = mod._tail_verify_cache.get(str(p))
    assert cached is not None
    cached_mtime_ns = p.stat().st_mtime_ns

    lines = p.read_text(encoding="utf-8").splitlines()
    old_size = p.stat().st_size
    assert '"v": 20000' in lines[-1]
    lines[-1] = lines[-1].replace('"v": 20000', '"v": 30000')  # 同长替换：size 不变
    p.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))  # 二进制写，避免换行符转换改变 size
    assert p.stat().st_size == old_size, "用例要求同长篡改（size 不变）"
    os.utime(p, ns=(cached_mtime_ns, cached_mtime_ns))  # 伪造 mtime 骗过命中检查

    assert lg.verify() is False
    with pytest.raises(LedgerCorruptionError):
        lg.append({"v": "after-tamper"})


def test_batch_append_tail_chain_continuous(tmp_path):
    """批量 append 后 tail hash 链连续：每租户 prev 指向上一条同租户 record_hash。"""
    from hero_quant.governance import ledger as mod
    from hero_quant.governance.ledger import Ledger

    p = tmp_path / "ledger.jsonl"
    lg = Ledger(p)
    n = 120
    for i in range(n):
        lg.append({"i": i}, tenant="tA" if i % 2 == 0 else "tB")
    entries = lg._read_all()
    assert len(entries) == n
    last_seen: dict = {}
    for e in entries:
        t = e["tenant"]
        expected_prev = last_seen.get(t, "sha256:genesis")
        if e["prev_hash"] != expected_prev:
            assert expected_prev == "sha256:genesis" and e["prev_hash"] in ("sha256:genesis", "0" * 64)
        last_seen[t] = e["record_hash"]
    assert lg.verify() is True
    cached = mod._tail_verify_cache.get(str(p))
    assert cached is not None and len(cached) == 5
    assert cached[2] == n and cached[3] == entries[-1]["record_hash"]
    tenants = cached[4]
    assert tenants["tA"][0] == 60 and tenants["tB"][0] == 60


def test_append_throughput_avg_lt_50ms(tmp_path, monkeypatch):
    """批量追加吞吐：单次 append 均值 < 50ms，且稳态下零全扫（增量短路生效）。"""
    from hero_quant.governance.ledger import Ledger

    p = tmp_path / "ledger.jsonl"
    lg = Ledger(p)
    lg.append({"warm": 1})

    calls = {"n": 0}
    orig = Ledger._verify_entries

    def counting(self, entries):
        calls["n"] += 1
        return orig(self, entries)

    monkeypatch.setattr(Ledger, "_verify_entries", counting)
    n = 300
    t0 = time.perf_counter()
    for i in range(n):
        lg.append({"i": i})
    dt = time.perf_counter() - t0
    assert dt / n < 0.05, f"avg append {dt / n * 1000:.2f}ms 超 50ms"
    assert calls["n"] == 0, "稳态追加必须全部命中增量短路、零全扫"
    assert lg.verify() is True
