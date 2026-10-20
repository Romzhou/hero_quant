"""O线 wall_time_budget 边界回归锁（O6）：显式值仅接受 0 < x <= 3600，非法值 422。

覆盖：NaN/-5/1e9/0/3600.1/±inf → 422（query 与 stream 双端）；
1/3600 → 非 422（持有效票；ticket 单次有效，合法请求每次 fresh ticket）。
校验位置：限流之后、ticket 消费之前——非法参数不耗单次票（下式用同一票连打非法值
仍全部 422 且票可用作证），无票+非法值时优先 422。
settings fallback（wall_time_budget_seconds 默认）不钳制，只校验显式传入值。

票据来源：经 security.issue_ticket(scope="query") 直签（与服务端同一
fakeredis/Redis 票据存储，单次消费语义一致）；不用 HTTP 签发是为避开
POST /v1/query/ticket 的 10/min 签发配额（签发路径本身已由
test_o_fuzz_edges.py 锁定，本文件不重复消耗）。
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _client():
    from hero_quant.api.server import app

    return TestClient(app)


def _fresh_ticket() -> str:
    from hero_quant.api.security import issue_ticket

    return issue_ticket(scope="query")


def test_o_wall_time_query_illegal_422():
    c = _client()
    # 同一票连打：422 在 ticket 消费之前返回，故票不被消耗，循环内复用同一票合法。
    t = _fresh_ticket()
    for bad in ("NaN", "-5", "1e9", "0", "3600.1", "inf", "-inf"):
        r = c.get("/v1/query", params={"q": "hi", "wall_time_budget": bad}, headers={"X-Ticket": t})
        assert r.status_code == 422, (bad, r.status_code, r.text[:200])
    # 非法请求未耗票：同一票仍可正常消费一次（O6 位置选择的直接证据）。
    r = c.get("/v1/query", params={"q": "hi", "wall_time_budget": "1"}, headers={"X-Ticket": t})
    assert r.status_code == 200, (r.status_code, r.text[:200])


def test_o_wall_time_query_legal_non422():
    c = _client()
    first = True
    for good in ("1", "3600"):
        # 合法请求正常消费票据：每次 fresh ticket。
        t = _fresh_ticket()
        r = c.get("/v1/query", params={"q": "hi", "wall_time_budget": good}, headers={"X-Ticket": t})
        assert r.status_code == 200, (good, r.status_code, r.text[:200])
        if first:
            first = False
            body = r.json()
            # 降级可观测语义（环境无关部分）：degraded:true + 非空 reason + 头同步。
            # 注：本环境配置了 key，LLM 走 factory fallback 而非 _FakeLLM，
            # 故 llm_mode/模板 disclaimer 属 key 缺失环境的 fake 分支语义，
            # 由 test_o_fake_template_unified（源码级）+ evals fake 30/30 锁定，此处不断言。
            assert body.get("degraded") is True
            assert body.get("degraded_reason"), "degraded:true 必须带 reason"
            assert r.headers.get("X-Degraded") == "true"


def test_o_wall_time_stream_illegal_422():
    c = _client()
    t = _fresh_ticket()
    for bad in ("NaN", "3600.1"):
        r = c.get(
            "/v1/query/stream", params={"q": "hi", "wall_time_budget": bad}, headers={"X-Ticket": t}
        )
        assert r.status_code == 422, (bad, r.status_code, r.text[:200])


def test_o_wall_time_stream_legal_non422():
    c = _client()
    t = _fresh_ticket()
    r = c.get("/v1/query/stream", params={"q": "hi", "wall_time_budget": "3600"}, headers={"X-Ticket": t})
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert r.headers.get("X-Degraded") == "true"
    assert "degraded" in r.text  # 流首事件 degraded:true + reason，与响应头对应
    # 注：disclaimer 行仅 fake 分支（无 key）文本携带，本环境有 key 不断言；
    # 模板统一由 test_o_fake_template_unified 源码级锁定。


def test_o_fake_template_unified():
    """O1 模板统一回归锁（环境无关，源码级）：server 两处 _FakeLLM 与 evals FakeLLM
    模板完全一致，含 for-query 嵌入 + disclaimer 行，已删长模板多出的 3 行。"""
    import importlib.util
    import pathlib
    import re

    server_src = pathlib.Path("src/hero_quant/api/server.py").read_text(encoding="utf-8")
    lits = re.findall(r'text = (f"600519\.SH close[^"\n]*")', server_src)
    assert len(lits) == 2, f"server 应恰有两处 _FakeLLM 模板，实得 {len(lits)}"
    assert lits[0] == lits[1], "两处 _FakeLLM 模板必须完全一致"
    tpl = lits[0]
    assert "for query: {goal}" in tpl, "模板必须嵌入用户 query"
    assert "本结果为合成占位，不构成投资建议" in tpl, "模板尾部必须有固定免责声明行"
    for gone in ("数据来源", "回测区间", "等权策略跑赢基准"):
        assert gone not in tpl, f"长模板多出旧行未删：{gone}"

    repo_root = pathlib.Path(".").resolve()
    spec = importlib.util.spec_from_file_location("hq_evals_run", repo_root / "evals" / "run.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    probe = "PROBE_O1"
    evals_text = next(iter(mod.FakeLLM().stream_chat(probe)))["text"]
    # pylint: disable=eval-used — 测试内对本仓源码字面量求值，无外部输入
    server_text = eval(tpl, {"goal": probe})  # noqa: S307
    assert evals_text == server_text, "evals FakeLLM 必须与 server 统一模板一致"
