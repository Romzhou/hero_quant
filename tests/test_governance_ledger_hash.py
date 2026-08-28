import pytest
from hero_quant.governance.ledger import compute_record_hash


def test_compute_hash_used_everywhere(tmp_path):
    h1 = compute_record_hash(1, "0" * 64, {"a": 1})
    assert h1.startswith("sha256:")
    # 追加的记录哈希必须与 compute_record_hash 同源（canonical JSON）
    from hero_quant.governance.ledger import Ledger, GENESIS_PREV_HASH

    ledger = Ledger(tmp_path / "ledger.jsonl")
    rec = {"b": 2, "a": 1, "z": "üñî"}
    obj = ledger.append(rec)
    # Ledger 内部应使用相同的 canonical 序列化，结果应与 compute_record_hash 派生一致
    expected_prefixed = compute_record_hash(1, GENESIS_PREV_HASH, rec)
    expected_hex = expected_prefixed.removeprefix("sha256:")
    assert obj["record_hash"] in (expected_hex, expected_prefixed), (
        f"ledger hash not unified: stored {obj['record_hash']!r} vs expected {expected_prefixed!r}"
    )
    assert ledger.verify() is True


def test_lock_not_swallowed(tmp_path):
    from hero_quant.governance.ledger import Ledger

    l = Ledger(tmp_path / "ledger.jsonl")
    l.append({"type": "test"})
    assert l.verify() is True
    # 锁失败不应被静默吞掉而继续无锁写
    from unittest import mock

    import hero_quant.governance.ledger as mod

    if mod.fcntl is not None:
        with mock.patch.object(mod.fcntl, "flock", side_effect=OSError("lock failed")):
            l2 = Ledger(tmp_path / "ledger2.jsonl")
            with pytest.raises(OSError):
                l2.append({"type": "test2"})
    else:
        # Windows 分支：msvcrt.locking 失败应抛
        if mod.msvcrt is not None:
            with mock.patch.object(mod.msvcrt, "locking", side_effect=OSError("lock failed")):
                l2 = Ledger(tmp_path / "ledger2.jsonl")
                with pytest.raises(OSError):
                    l2.append({"type": "test2"})
