"""O4 回归锁：Redis 不可达时签发端 fail-closed（429 / 503），符合 G-06 精神，**非缺陷**。

依据（读源码复核，2026-10-21，W2 线；与 docs/fuzz-report.md §4 O4 对齐）：

- `src/hero_quant/infra/redis.py:761-803` `RateLimiter.try_acquire_sync`：
  - 无后端（`get_redis_sync()` → None）→ **直接 False**（fail-closed，不放行，`:764-767`）；
  - 后端抛异常 → 上抛（`get_redis_sync()` 在 try 块之外，`:763`）。
- `src/hero_quant/api/server.py:1640-1641`：try_acquire_sync 返回 False → 429 且**不签票**；
  `:1642-1651`：限流器异常 → 503 ``{"detail": "Rate limiter unavailable"}``。
- `src/hero_quant/infra/redis.py:220-237` `_require_real_redis_allowed()`：
  fakeredis 回退**仅**在 ``HERO_ENV == "development"`` 或 ``PYTEST_CURRENT_TEST`` 存在时允许；
  其余（production / staging / 未设）一律 fail-closed（返回 None → 限流/票据随即拒绝）。
- `src/hero_quant/api/security.py:262-280` `issue_ticket`：Redis 不可用 → 内存回退
  （票据存储降级，但**签发入口**在限流层就已 fail-closed 拒绝）。

**陷阱（务必知悉）**：pytest 进程必定带 `PYTEST_CURRENT_TEST` ⇒ fakeredis 回退在 pytest 内
**永远**被允许 ⇒ 「Redis 不可达 → 429」**不能**靠改环境变量触发，必须主动注入不可用
（monkeypatch `hero_quant.infra.redis.get_redis_sync`）；门禁函数本身另单独锁定。

本文件**不动** `src/hero_quant/infra/redis.py`：只锁行为，不改实现。
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient


def _client():
    from hero_quant.api.server import app

    return TestClient(app)


def test_o4_ticket_429_when_redis_backend_missing(monkeypatch, caplog):
    """Redis 无后端 → 签发端 fail-closed 429，且**不签票**。

    429 的成因必须是「无后端 → 拒绝」而非「配额耗尽」：用 caplog 锚定
    `infra/redis.py:731` 的 ``redis.ratelimit_no_backend_reject`` 告警。
    """
    from hero_quant.infra.redis import RateLimiter

    monkeypatch.setattr("hero_quant.infra.redis.get_redis_sync", lambda: None)
    c = _client()
    with caplog.at_level(logging.WARNING, logger="hero_quant.infra.redis"):
        r = c.post("/v1/query/ticket")
    assert r.status_code == 429, (r.status_code, r.text[:200])
    body = r.json()
    assert "ticket" not in body, body
    assert any(
        "ratelimit_no_backend_reject" in rec.getMessage() for rec in caplog.records
    ), [rec.getMessage() for rec in caplog.records]
    # 限流器层同理：无后端 → False（不放行）
    assert RateLimiter().try_acquire_sync("o4:probe:no_backend", 1, 60) is False


def test_o4_ticket_503_when_redis_backend_raises(monkeypatch):
    """Redis 后端抛异常 → 签发端 503（限流器不可用，fail-closed，不签票）。"""
    monkeypatch.setattr(
        "hero_quant.infra.redis.get_redis_sync",
        lambda: (_ for _ in ()).throw(RuntimeError("redis backend down (simulated)")),
    )
    c = _client()
    r = c.post("/v1/query/ticket")
    assert r.status_code == 503, (r.status_code, r.text[:200])
    assert r.json().get("detail") == "Rate limiter unavailable", r.text[:200]


def test_o4_security_layer_memory_fallback_when_redis_missing(monkeypatch):
    """分层语义：Redis 不可用时，**入口**（签发端点）fail-closed 拒绝；
    而底层 `issue_ticket` 降级到进程内存储（单次消费语义保持）。

    二者不矛盾：入口的 429 来自限流层无后端（防无保护超发），票据存储只是降级。
    """
    monkeypatch.setattr("hero_quant.infra.redis.get_redis_sync", lambda: None)
    from hero_quant.api.security import consume_ticket_with_user, issue_ticket

    t = issue_ticket(scope="query")
    assert t
    ok, uid = consume_ticket_with_user(t, require_scope="query")
    assert ok is True
    assert uid == ""
    # 单次消费：同一张票不可重用
    ok2, _uid2 = consume_ticket_with_user(t, require_scope="query")
    assert ok2 is False


def test_o4_real_redis_gate_env_matrix(monkeypatch):
    """生产门禁 `_require_real_redis_allowed()` 的 env 矩阵（`infra/redis.py:220-237`）。

    - HERO_ENV=development → 允许 fakeredis 回退；
    - 无 HERO_ENV 且无 PYTEST_CURRENT_TEST → fail-closed（拒绝回退）；
    - PYTEST_CURRENT_TEST 存在（pytest 进程内）→ 允许 —— 这正是
      「pytest 里回退永远被允许、故 429 不能靠 env 触发」的陷阱来源；
    - HERO_ENV=production 但处于 pytest → 仍允许（门只认 development / pytest 两个条件）。
    """
    from hero_quant.infra.redis import _require_real_redis_allowed

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("HERO_ENV", "development")
    assert _require_real_redis_allowed() is True

    monkeypatch.delenv("HERO_ENV", raising=False)
    assert _require_real_redis_allowed() is False

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "dummy::test (call)")
    assert _require_real_redis_allowed() is True

    monkeypatch.setenv("HERO_ENV", "production")
    assert _require_real_redis_allowed() is True


def test_o4_get_redis_sync_none_when_gate_closed(monkeypatch):
    """门禁关闭且无 Redis DSN 时，`get_redis_sync()` 必须返回 None（fail-closed）。

    注意 `get_redis_sync()` 会先返回已缓存的客户端（`infra/redis.py:242-247`），
    故这里先清缓存，否则测的是缓存而不是门禁。
    """
    from hero_quant.config.settings import Settings

    if Settings().redis_dsn:
        pytest.skip("环境配置了真实 Redis DSN：门禁关闭时走 ping 失败分支，语义不同")

    import hero_quant.infra.redis as rmod

    monkeypatch.delenv("HERO_ENV", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    rmod.clear_redis_instance()
    assert rmod.get_redis_sync() is None
    # 门禁重新打开（pytest 内）→ 回退可用，且不缓存坏客户端
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "dummy::test (call)")
    assert rmod.get_redis_sync() is not None
