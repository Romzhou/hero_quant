"""O3 回归锁：ticket scope 归一化（``"query "`` → ``"query"``）是**有意设计**，不是越权。

复核结论（读源码复核，2026-10-21，W2 线；与 docs/fuzz-report.md §4 O3 对齐）：

- 签发端唯一入口 `src/hero_quant/api/server.py:1670-1674`：先 `_normalize_scope(scope)`，
  归一化失败（None）→ 400 fail-closed；**归一化后的值**才写进票据载荷（`:1710`）。
- `src/hero_quant/api/security.py:101-108` `_normalize_scope`：strip 后做白名单成员判定
  （`_TICKET_SCOPES = {"query","metrics","stream"}`，`:98`）→ 非法值返回 None（fail-closed）。
- `src/hero_quant/api/security.py:253-254` `issue_ticket` 二次归一化（同一函数，同一白名单）。
- 消费端 `src/hero_quant/api/security.py:111-136` `_parse_ticket_scope`：**不** strip，
  只认载荷里的精确分域值；载荷里写的永远是归一化后的 ``"query"``。

⇒ 原始 scope 字符串**从不**流入下游；空白归一化 = 单一入口的一致规范化，
不存在"原始串绕过白名单"的越权面。故判定为「选项 A：语义故意放行 → 写锁测试」，
本次**不改 src**（改 src 会推翻既有锁 `tests/fuzz/test_o_fuzz_edges.py:25-28`
与 docs/fuzz-report.md §3 的复现路径）。

锁定内容：
1. `_normalize_scope`：合法值 + 首尾空白 → 规范值；非法值（queryx/空/纯空白/大写/非 str）→ None；
2. 归一化后的票据**恰好**是 query 分域（query 面可消费、metrics 面 fail-closed 拒绝），
   既没被放大成 legacy 全域票，也没被缩小成别的分域；
3. 签发端 HTTP 层：``scope=query `` / `` query`` / ``query`` → 200；
   ``queryx`` / 空串 / 纯空白 / 大写 → 400 且不签票；
4. 载荷里塞原始 ``"query "``（伪造 / 非归一化写入）→ 消费端**不**识别为 query 分域，
   与「载荷里完全没有 scope」完全等价（G-08 legacy 兼容），
   即原始串不会以 query 身份流向下游。
"""

from __future__ import annotations

import json
import secrets

import pytest
from fastapi.testclient import TestClient


def _client():
    from hero_quant.api.server import app

    return TestClient(app)


@pytest.fixture()
def _no_shared_quota(monkeypatch):
    """签发/查询限流是**跨用例共享状态**（ticket 10/min、query 20/min，按 IP 桶；
    `src/hero_quant/api/server.py:1627-1641` 与 `:504-551`），与 scope 语义无关。

    这里把 `RateLimiter.try_acquire_sync` 短路为 True：不消耗共享配额、不被同 session
    其它用例挤掉，保证本文件断言确定性（monkeypatch 在用例结束自动还原）。
    """
    from hero_quant.infra.redis import RateLimiter

    monkeypatch.setattr(RateLimiter, "try_acquire_sync", lambda self, *a, **kw: True)


def test_o3_normalize_scope_whitelist_canonical():
    """白名单 + 空白规范化 + 非法值 fail-closed（None）。"""
    from hero_quant.api.security import _TICKET_SCOPES, _normalize_scope

    assert _TICKET_SCOPES == frozenset({"query", "metrics", "stream"})
    # 合法分域（含首尾空白）→ 规范值
    assert _normalize_scope("query") == "query"
    assert _normalize_scope("query ") == "query"
    assert _normalize_scope(" query") == "query"
    assert _normalize_scope("\tquery\n") == "query"
    assert _normalize_scope("metrics ") == "metrics"
    assert _normalize_scope(" stream") == "stream"
    # 非法分域 → None（fail-closed，由签发端转 400）
    for bad in (
        "queryx",
        "query metrics",
        "",
        "   ",
        "QUERY",
        "Query",
        "metrics;DROP",
        "1",
        "none",
        None,
        1,
        1.5,
        True,
        ["query"],
    ):
        assert _normalize_scope(bad) is None, bad


def test_o3_issued_ticket_scope_is_exactly_query():
    """归一化后的票据**恰好**是 query 分域：query 面可消费，metrics 面 fail-closed 拒绝。

    这条同时排除两种误判：既没被放大成 legacy 全域票（否则 metrics 面也放行），
    也没被归一成别的分域（否则 query 面反而不通）。
    """
    from hero_quant.api.security import consume_ticket_with_user, issue_ticket

    t_ok = issue_ticket(scope="query ")
    ok, uid = consume_ticket_with_user(t_ok, require_scope="query")
    assert ok is True, "scope='query ' 归一化后应可作为 query 分域票消费"
    assert isinstance(uid, str)

    t_bad = issue_ticket(scope="query ")
    ok2, _uid2 = consume_ticket_with_user(t_bad, require_scope="metrics")
    assert ok2 is False, "query 分域票不得用于 metrics 面（否则说明归一化退化成 legacy 全域票）"


def test_o3_ticket_endpoint_scope_normalization(_no_shared_quota):
    """签发端 HTTP 层：空白 scope 归一化放行（200），非法 scope fail-closed（400 且不签票）。"""
    c = _client()
    for raw in ("query", "query ", " query", "\tquery"):
        r = c.post("/v1/query/ticket", params={"scope": raw})
        assert r.status_code == 200, (raw, r.status_code, r.text[:200])
        assert r.json().get("ticket"), (raw, r.text[:200])
    for bad in ("queryx", "", "   ", "QUERY", "metrics;DROP", "1"):
        r = c.post("/v1/query/ticket", params={"scope": bad})
        assert r.status_code == 400, (bad, r.status_code, r.text[:200])
        assert "ticket" not in r.json(), (bad, r.text[:200])


def test_o3_raw_scope_in_payload_not_recognized_as_query():
    """载荷里塞原始 ``"query "`` → 消费端**不**识别为 query 分域。

    `_parse_ticket_scope`（`src/hero_quant/api/security.py:111-136`）只认白名单里的**精确**
    分域值，载荷里写 ``"query "`` → 返回 None（= 无分域/legacy），即原始串不会以 query 身份
    流向下游。按 G-08 契约，legacy 无分域票在迁移期放行（`security.py:139-151`），
    故其效果与「载荷里完全没有 scope」**完全等价**（不因 ``"query "`` 获得任何额外权限）。

    注：伪造载荷的前提是已能写入票据存储（Redis = 可信基础设施边界），本用例只在
    消费逻辑层锁定判定，不对票据存储做写入。
    """
    from hero_quant.api.security import _parse_ticket_scope, _scope_allowed

    raw_scope_payload = json.dumps({"user_id": "", "scope": "query "}, separators=(",", ":"))
    no_scope_payload = json.dumps({"user_id": ""}, separators=(",", ":"))

    # 原始 "query " 不被识别为任何分域
    assert _parse_ticket_scope(raw_scope_payload) is None
    # 与「载荷里完全没有 scope」等价（同为 legacy 无分域票）
    assert _parse_ticket_scope(no_scope_payload) is None
    assert _scope_allowed(
        _parse_ticket_scope(raw_scope_payload), "metrics"
    ) == _scope_allowed(_parse_ticket_scope(no_scope_payload), "metrics")
    # 对照：规范值 "query" 是精确分域票，metrics 面被拒
    assert _parse_ticket_scope(json.dumps({"user_id": "", "scope": "query"})) == "query"
    assert _scope_allowed("query", "metrics") is False


def test_o3_forged_payload_raw_scope_consumes_as_legacy(_no_shared_quota):
    """端到端：伪造载荷（原始 ``"query "``）写入票据存储 → 消费端按 legacy 处理。

    证据锚点：消费路径 `src/hero_quant/api/server.py:1359` → `consume_ticket_with_user`
    → `security.py:301/320` `_parse_ticket_scope` + `_scope_allowed`。
    仅在票据存储为 fakeredis（进程内）时注入伪造载荷，避免污染真实 Redis。
    """
    fakeredis = pytest.importorskip("fakeredis")
    from hero_quant.infra.redis import get_redis_sync

    r = get_redis_sync()
    if r is None or not isinstance(r, fakeredis.FakeRedis):
        pytest.skip("票据存储非 fakeredis（可能是真实 Redis），跳过伪造载荷注入")
    payload = json.dumps({"user_id": "", "scope": "query "}, separators=(",", ":"))
    forged = "forged-o3-" + secrets.token_urlsafe(8)
    r.set(f"hero:ticket:{forged}", payload, ex=60)

    from hero_quant.api.security import consume_ticket_with_user

    ok, _uid = consume_ticket_with_user(forged, require_scope="query")
    # 原始 "query " 未被识别为 query 分域 → 按 legacy 无分域票处理（G-08 迁移期放行），
    # 与「无 scope 载荷」一致：不会以 query 身份流通，也不会因伪造而额外放行 metrics 面。
    assert ok is True
    # 对照：真正的 query 分域票被 metrics 面拒绝（证明上一条不是"全放行"）
    from hero_quant.api.security import issue_ticket

    t = issue_ticket(scope="query")
    ok2, _uid2 = consume_ticket_with_user(t, require_scope="metrics")
    assert ok2 is False
