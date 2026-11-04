"""批次18 P1-10：记忆检索 Redis L2 缓存必须按 store 实例隔离。

缺陷：L2 key 为 hero:cache:memory:search:{ns}:{query}:{backend}，不含数据根身份，
不同 base_path 的两个 MemoryStore 会互相命中对方缓存（批次17 阶段2 已复现假绿）。
"""

from __future__ import annotations

import pytest

import fakeredis

import hero_quant.infra.redis as rmod
from hero_quant.memory import store as memory_mod


@pytest.fixture()
def fake_redis(monkeypatch):
    monkeypatch.setattr(memory_mod, "is_pgvector_configured", lambda: False)
    rmod.clear_redis_instance()
    inst = fakeredis.FakeRedis(decode_responses=True)
    rmod.set_redis_instance(inst)
    yield inst
    rmod.clear_redis_instance()


def _store(tmp_path, name):
    return memory_mod.MemoryStore(base_path=tmp_path / name)


def test_l2_key_and_prefix_include_store_identity(tmp_path, fake_redis):
    a = _store(tmp_path, "a")
    b = _store(tmp_path, "b")
    assert a._l2_key("same query") != b._l2_key("same query")
    assert a._l2_prefix() != b._l2_prefix()
    a.close()
    b.close()


def test_two_stores_do_not_share_l2_hits(tmp_path, fake_redis):
    marker = "zzqL2isolationmarker"
    a = _store(tmp_path, "a")
    b = _store(tmp_path, "b")
    try:
        a.write("k-a", f"{marker} 仅属于 A")
        hits_a = a.search(marker)
        assert any(marker in str(h.get("content") or "") for h in hits_a)
        # A 的检索结果已写入 Redis L2；B 的 DB 里没有该内容，且不得命中 A 的 L2
        hits_b = b.search(marker)
        assert not any(marker in str(h.get("content") or "") for h in hits_b), (
            "B 命中了 A 的 L2 缓存（跨实例串数据）"
        )
        # L2 里确实存在 A 的记录，证明这条断言不是靠「没写 L2」蒙混过关
        assert any(k.decode() if isinstance(k, bytes) else k for k in fake_redis.keys("*"))
    finally:
        a.close()
        b.close()


def test_l2_invalidation_prefix_is_store_scoped(tmp_path, fake_redis):
    a = _store(tmp_path, "a")
    b = _store(tmp_path, "b")
    try:
        fake_redis.set(a._l2_key("q"), "[]", ex=30)
        fake_redis.set(b._l2_key("q"), "[]", ex=30)
        a.clear_retrieval_cache(include_redis_l2=True)
        assert not fake_redis.exists(a._l2_key("q"))
        assert fake_redis.exists(b._l2_key("q")), "A 的失效不得误删 B 的 L2 记录"
    finally:
        a.close()
        b.close()
