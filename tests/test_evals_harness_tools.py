"""real 模式评测口径：客户端必须与生产 `/v1/query` 同序 `bind_tools`，usage 必须可追溯。

背景（上一轮 22/50 的真因）：`evals/run_golden.py` 拿 `LLMGateway` 当 llm，网关没有
`bind_tools`，模型收不到工具 schema → `tool_selection` tp=0/fn=55，
`exact_match_rate` 只统计了「期望为空」的用例。那是口径缺陷，不是模型能力。

本文件全部离线：fake factory / fake chat，不联网、不读任何真实 key。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals import metrics as M  # noqa: E402
from evals import run_golden as RG  # noqa: E402
from hero_quant.llm.client import LLMClient  # noqa: E402
from hero_quant.llm.cost import UsageLedger, build_usage  # noqa: E402

FAKE_KEY = "sk-offline-unit-test-not-a-real-key"
UNPRICED_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"


def _register_tools() -> None:
    """注册真实工具（离线，纯本地注册表），保证 get_definitions() 非空。"""
    import hero_quant.tools.backtest  # noqa: F401
    import hero_quant.tools.correlation  # noqa: F401
    import hero_quant.tools.market_data  # noqa: F401
    import hero_quant.tools.presentation  # noqa: F401
    import hero_quant.tools.quantlib_tool  # noqa: F401


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "api_key": FAKE_KEY,
        "openai_api_key": None,
        "llm_base_url": "http://127.0.0.1:3001/v1",
        "llm_model": "fake-model-1",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _Chat:
    """最小 chat 替身：记录 `bind_tools` 入参，可按需抛错（模拟绑定失败）。"""

    def __init__(self, *, bind_error: BaseException | None = None) -> None:
        self.bind_error = bind_error
        self.bind_calls = 0
        self.bound_defs: list[dict] | None = None

    def bind_tools(self, defs):
        self.bind_calls += 1
        self.bound_defs = list(defs)
        if self.bind_error is not None:
            raise self.bind_error
        self.model = getattr(self, "model", "fake-model-1")
        return self

    def stream_chat(self, prompt, timeout=None):
        yield {"type": "text", "text": "mock"}


class _Factory:
    """factory 替身：与 LLMFactory 同形状（provider / model_for_stage / create）。"""

    provider = "openai"

    def __init__(self, chat: _Chat, *, model_name: str = "fake-model-1") -> None:
        self.chat = chat
        self.model_name = model_name
        self.created: dict = {}

    def model_for_stage(self, stage: str):
        assert stage == "plan"  # 与生产 server.py 一致：链A 用 plan 槽位
        return SimpleNamespace(name=self.model_name)

    def create(self, **kwargs):
        self.created.update(kwargs)
        return self.chat


def test_real_build_binds_non_empty_tool_definitions():
    """real 构建必须调 bind_tools 且传入非空 defs（绑定口径 = 生产 /v1/query）。"""
    _register_tools()
    chat = _Chat()
    factory = _Factory(chat)

    client = RG.build_real_llm(_settings(), factory=factory)

    assert client.llm is not None
    # 非 LLMClient 必须被包一层，否则生产同款 `_chat.bind_tools` 路径走不到
    assert isinstance(client.llm, LLMClient)
    assert chat.bind_calls == 1
    assert chat.bound_defs, "real 构建必须把非空工具 schema 传给 bind_tools"
    assert chat.bound_defs[0]["type"] == "function"
    assert "get_market_data" in {d["function"]["name"] for d in chat.bound_defs}
    assert client.provider["tools_bound"] is True
    assert client.provider["tools_available"] == len(chat.bound_defs)
    assert client.warnings == []


def test_real_build_matches_production_create_kwargs():
    """create 的参数必须与生产同款（streaming/temperature/base_url/model）。"""
    _register_tools()
    factory = _Factory(_Chat())

    RG.build_real_llm(_settings(), factory=factory)

    assert factory.created["model"] == "fake-model-1"
    assert factory.created["api_key"] == FAKE_KEY
    assert factory.created["base_url"] == "http://127.0.0.1:3001/v1"
    assert factory.created["streaming"] is RG.REAL_STREAMING is True
    assert factory.created["temperature"] == RG.REAL_TEMPERATURE == 0.2


def test_bind_failure_marks_tools_bound_false_and_never_raises():
    """绑定失败：不抛、标 tools_bound=false，且评测继续跑完并把失败记成分类。"""
    _register_tools()
    chat = _Chat(bind_error=RuntimeError("bind exploded"))
    client = RG.build_real_llm(_settings(), factory=_Factory(chat))

    assert client.llm is not None, "绑定失败不得让整轮评测无法开始"
    assert client.provider["tools_bound"] is False
    assert client.provider["bind_warnings"]
    assert any(w.startswith("bind_chat_failed") for w in client.provider["bind_warnings"])
    assert "tools_bound" in client.provider and client.provider["tools_bound"] is False

    class _DeadBackend:
        def stream_chat(self, prompt, timeout=None):
            raise RuntimeError("backend unreachable")
            yield  # pragma: no cover - 生成器签名占位

    row = RG.run_case(RG.load_golden()[0], _DeadBackend())

    # 评测继续跑完：不抛异常，落成一条带失败分类的行（AgentLoop 内部会重试/收敛）
    assert row["passed"] is False
    assert row["failure_class"] == M.failure_class(row["failures"])
    assert row["failure_class"] != "pass"


def test_missing_key_skips_without_building_client():
    """无 key → 如实 skip（no_llm_backend），不得偷偷建客户端或伪造通过率。"""
    factory = _Factory(_Chat())

    client = RG.build_real_llm(_settings(api_key=None, openai_api_key=None), factory=factory)

    assert client.llm is None
    assert client.skip_reason == "no_llm_backend"
    assert factory.created == {}


def test_openai_api_key_is_accepted_as_fallback():
    """`api_key` 缺失时用 `openai_api_key`（与生产 `s.api_key or s.openai_api_key` 同序）。"""
    _register_tools()
    factory = _Factory(_Chat())

    client = RG.build_real_llm(_settings(api_key=None, openai_api_key=FAKE_KEY), factory=factory)

    assert client.llm is not None
    assert factory.created["api_key"] == FAKE_KEY


def test_usage_prefers_backend_counters_over_estimation():
    """后端给了 usage：tokens 直接采信、estimated=False（不覆盖、不估算）。"""
    usage = build_usage(
        None,
        "600519.SH 最近 20 日收盘价",
        "略",
        provider="openai",
        model="gpt-4o-mini",
        usage={"input_tokens": 120, "output_tokens": 30},
    )

    ledger = UsageLedger()
    ledger.add(usage)
    summary = ledger.summary()

    assert usage.estimated is False
    assert (usage.input_tokens, usage.output_tokens) == (120, 30)
    assert summary["total_tokens"] == 150
    assert summary["priced_calls"] == 1
    assert summary["unpriced_calls"] == 0
    assert summary["total_cost_usd"] is not None


def test_usage_missing_is_estimated_and_unpriced_not_zero():
    """`LoopResult.llm_usage=None`：标 estimated=True，catalog 外模型 cost=null 而非 0。"""
    usage = build_usage(
        None,
        "600519.SH 的收盘价是多少",
        "600519.SH 收盘价 1680.2，不构成投资建议",
        provider="openai",
        model=UNPRICED_MODEL,
    )

    ledger = UsageLedger()
    ledger.add(usage)
    summary = ledger.summary()

    assert usage.estimated is True
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0
    assert usage.cost_usd is None, "catalog 外模型不得猜价，更不得算 0"
    assert summary["unpriced_calls"] == 1
    assert summary["total_cost_usd"] is None
    assert summary["unpriced_models"] == [UNPRICED_MODEL]


def test_aggregate_surfaces_estimated_and_unpriced_counts():
    """聚合口径与现有 metrics 一致：估算数与未定价数都要能被读出来。"""
    usage = build_usage(None, "q", "answer", provider="openai", model=UNPRICED_MODEL)
    rows = [
        {
            "category": "market_data",
            "passed": False,
            "elapsed_ms": 12.0,
            "failure_class": "content_gap",
            "expected_tools": ["get_market_data"],
            "tools_called": [],
            "expect_grounding": None,
            "grounding_verified": False,
            "usage": usage.to_dict(),
            "cost_usd": usage.cost_usd,
        }
    ]

    cost = M.aggregate(rows)["quality_metrics"]["cost"]

    assert cost["estimated_cases"] == 1
    assert cost["unpriced_cases"] == 1
    assert cost["priced_cases"] == 0
    assert cost["total_usd"] is None
    assert cost["by_model"][f"openai/{UNPRICED_MODEL}"]["total_tokens"] > 0


def test_run_case_capture_feeds_backend_usage_into_ledger():
    """真实链路：run_case 回填 res.llm_usage → build_usage 采信后端计量（estimated=False）。"""
    case = next(c for c in RG.load_golden() if c["id"] == "ZH-01")

    class _MeteredLLM:
        def stream_chat(self, prompt, timeout=None):
            yield {
                "type": "text",
                "text": "该标的估值处于历史 60% 分位，不构成投资建议。",
                "usage_metadata": {"input_tokens": 40, "output_tokens": 12},
            }

    capture: dict = {}
    RG.run_case(case, _MeteredLLM(), None, capture=capture)

    assert capture["llm_usage"] == {"input_tokens": 40, "output_tokens": 12}
    usage = build_usage(
        None,
        str(case["query"]),
        str(capture["text"]),
        provider="openai",
        model="gpt-4o-mini",
        usage=capture["llm_usage"],
    )
    assert usage.estimated is False
    assert (usage.input_tokens, usage.output_tokens) == (40, 12)


def test_fake_mode_row_shape_unchanged_without_capture():
    """fake 模式不传 capture：行结构/键集保持原样（CI 门依赖 fake 产物）。"""
    case = next(c for c in RG.load_golden() if c["id"] == "ZH-01")

    row = RG.run_case(case, RG.FakeLLM())

    assert set(row) == {
        "id",
        "category",
        "passed",
        "failures",
        "failure_class",
        "elapsed_ms",
        "reason",
        "text_preview",
        "expected_tools",
        "tools_called",
        "expect_grounding",
        "grounding_verified",
    }


def test_provider_metadata_never_leaks_key_or_auth_header():
    """provider 元数据只写 host:port，绝不落 key / Authorization。"""
    _register_tools()
    client = RG.build_real_llm(_settings(), factory=_Factory(_Chat()))

    blob = json.dumps(client.provider, ensure_ascii=False)

    assert FAKE_KEY not in blob
    assert "Authorization" not in blob
    assert "Bearer" not in blob
    assert client.provider["base_url_host"] == "127.0.0.1:3001"
    assert client.provider["chain"] == ["openai"]
    assert client.provider["active"] == "openai"


def test_base_url_host_strips_userinfo_path_and_query():
    """base_url 可能内嵌凭证：只保留 host[:port]。"""
    assert RG.base_url_host("http://user:secret@localhost:3001/v1?api_key=abc") == "localhost:3001"
    assert RG.base_url_host("https://api.example.com") == "api.example.com"
    assert RG.base_url_host(None) is None
    assert RG.base_url_host("") is None
