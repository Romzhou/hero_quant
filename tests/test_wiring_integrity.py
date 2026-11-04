"""阶段3 接线护栏：真实装配（TestClient + 真 lifespan），不许 mock。

AGENTS §11 三层：
- 层1 非 None：app.state 上所有装配对象存在；
- 层2 精确类型：type(x).__module__ 用模块全路径，挡「同名类看起来接上了」的静默错误；
- 层3 行为探针：真实方法调用 + 返回结构断言，挡「类型对但实现空转」。

关于 grounding_ledger：账本是**每请求**对象（首个 ingest 会冻结 symbol 快照），
挂全局单例会让 A 请求的证据污染 B 请求的校验，因此 app.state 持有的是工厂
（grounding_ledger_factory），在此调用工厂做行为探针。

audit_store / session_store 依赖真 PG 连接池：无 DSN 时该用例按仓库既有 PG guard
口径跳过（不是为变绿放宽断言），scripts/test_local.sh 注入 DSN 后必跑。
"""

from __future__ import annotations

import pytest

from conftest import pg_guard_reason

_PG_GUARD_REASON = pg_guard_reason()

_ALWAYS_KEYS = (
    "memory_store",
    "session_buffer",
    "dedup_store",
    "grounding_ledger_factory",
    "secrets_provider",
    "pricing_provider",
)

_EXPECTED_MODULES = {
    "memory_store": "hero_quant.memory.store",
    "session_buffer": "hero_quant.agent.memory.store",
    "dedup_store": "hero_quant.governance.dedup",
    "grounding_ledger_factory": "hero_quant.agent.grounding",
    "secrets_provider": "hero_quant.secrets.env_provider",
    "pricing_provider": "hero_quant.pricing.catalog_provider",
}


@pytest.fixture()
def wired_app(monkeypatch, tmp_path):
    """真实应用 + 真实 lifespan；数据根指向 tmp_path 隔离磁盘副作用。"""
    monkeypatch.setenv("HERO_DATA_DIR", str(tmp_path))
    for key in ("HERO_API_KEY", "OPENAI_API_KEY", "HERO_LLM_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    from hero_quant.config.settings import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from hero_quant.api import server as srv

    # 真 TestClient 跑完真实 lifespan（含真 PG 池装配与关停），全程零 mock。
    with TestClient(srv.app, client=("wiring-integrity", 50001)):
        yield srv.app


def _module_of(obj) -> str:
    return obj.__module__ if isinstance(obj, type) else type(obj).__module__


def test_layer1_all_wired_objects_are_non_none(wired_app):
    for key in _ALWAYS_KEYS:
        assert getattr(wired_app.state, key, None) is not None, f"app.state.{key} missing"


def test_layer2_exact_module_paths(wired_app):
    for key, expected in _EXPECTED_MODULES.items():
        obj = getattr(wired_app.state, key)
        assert _module_of(obj) == expected, f"{key} 来自 {_module_of(obj)}，不是 {expected}"


def test_layer3_behavior_probes(wired_app):
    st = wired_app.state

    # 1) 持久化记忆：写入能被同一实例召回（文件 + FTS5 真落盘）
    st.memory_store.write("wiring-probe", "zzqwiringprobe 落盘探针")
    hits = st.memory_store.recall("zzqwiringprobe")
    assert isinstance(hits, list) and hits
    assert any("zzqwiringprobe" in str(h.get("content") or "") for h in hits)

    # 2) 去重账本：PENDING → SUCCESS 的真实状态机
    key = "wiring-probe:wf:s1:t_probe:biz1"
    assert st.dedup_store.insert_pending(key, "t_probe") is True
    st.dedup_store.mark_success(key, {"ok": True})
    rec = st.dedup_store.get(key)
    assert rec is not None and rec.get("status") == "SUCCESS"

    # 3) 证据账本工厂：ingest → assert_price → render_block 全链路
    ledger = st.grounding_ledger_factory()
    bars = [{"date": "2026-08-03", "open": 99.0, "high": 101.0, "low": 98.0, "close": 100.0, "volume": 10}]
    ledger.ingest("WIRING", bars, {"source": "wiring-probe", "unit": "shares"})
    ledger.assert_price("WIRING", 100.0)
    assert ledger.render_block().startswith("## Ground Truth")

    # 4) 密钥 provider：describe 结构可观测（不含任何密钥值）
    desc = st.secrets_provider.describe()
    assert isinstance(desc, dict) and desc.get("backend") == "env"
    assert isinstance(desc.get("names"), (list, tuple))

    # 5) 定价 provider：真实价卡枚举，结构字段可断言
    cards = st.pricing_provider.list_cards()
    assert isinstance(cards, list) and cards
    assert all(hasattr(c, "model_id") and hasattr(c, "input_usd_per_mtok") for c in cards)


@pytest.mark.skipif(_PG_GUARD_REASON is not None, reason=_PG_GUARD_REASON or "PG guard")
def test_pg_backed_stores_are_wired_with_exact_modules(wired_app):
    """audit_store / session_store 只在真 PG 环境装配，层1 + 层2 全跑。"""
    for key, expected in (
        ("audit_store", "hero_quant.audit.store"),
        ("session_store", "hero_quant.session.store"),
    ):
        obj = getattr(wired_app.state, key, None)
        assert obj is not None, f"app.state.{key} missing"
        assert _module_of(obj) == expected, f"{key} 来自 {_module_of(obj)}，不是 {expected}"
