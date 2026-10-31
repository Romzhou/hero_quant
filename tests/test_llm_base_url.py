"""HERO_LLM_BASE_URL：OpenAI 兼容网关根地址的解析/校验 → 网关 → 链A 创建点。

三层覆盖：
1. `Settings`：空值不覆盖（None），合法值去尾斜杠，非 http(s) / userinfo / 缺 host 直接 ValueError；
2. `LLMGateway.from_settings`：base_url 只落在 primary ProviderSpec，回退 provider 不带（不串网关）；
3. `api/server.py` 链A 三处创建点：factory.create 实际收到 settings 里的 base_url。
"""

from __future__ import annotations

import pytest

from hero_quant.config.settings import Settings
from hero_quant.llm.gateway import LLMGateway, ProviderSpec


# ---------------------------------------------------------------- Settings 解析
def test_llm_base_url_defaults_to_none(monkeypatch):
    monkeypatch.delenv("HERO_LLM_BASE_URL", raising=False)
    assert Settings().llm_base_url is None


def test_llm_base_url_blank_means_no_override(monkeypatch):
    monkeypatch.setenv("HERO_LLM_BASE_URL", "   ")
    assert Settings().llm_base_url is None


def test_llm_base_url_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("HERO_LLM_BASE_URL", "http://localhost:3001/v1/")
    assert Settings().llm_base_url == "http://localhost:3001/v1"


def test_llm_base_url_keeps_https_and_drops_all_trailing_slashes(monkeypatch):
    monkeypatch.setenv("HERO_LLM_BASE_URL", "  HTTPS://gateway.example.com/openai/v1//  ")
    assert Settings().llm_base_url == "https://gateway.example.com/openai/v1"


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://gateway.example.com/v1",  # 非 http(s)
        "javascript:alert(1)",  # 无 scheme + 脚本伪协议
        "gateway.example.com/v1",  # 缺 scheme（避免被 SDK 当成相对路径）
        "http://",  # 缺 host
        "http://user:pass@gateway.example.com/v1",  # 内嵌 userinfo（凭据只走 HERO_API_KEY）
    ],
)
def test_llm_base_url_rejects_unsafe_values(monkeypatch, raw):
    monkeypatch.setenv("HERO_LLM_BASE_URL", raw)
    with pytest.raises(ValueError):
        Settings()


def test_llm_base_url_error_never_echoes_userinfo(monkeypatch):
    """报错信息不得回显 userinfo 里的口令（否则凭据会随 traceback 进日志）。"""
    monkeypatch.setenv("HERO_LLM_BASE_URL", "http://robot:s3cr3t-pw@gateway.example.com/v1")
    with pytest.raises(ValueError) as ei:
        Settings()
    assert "s3cr3t-pw" not in str(ei.value)
    assert "robot" not in str(ei.value)


# ---------------------------------------------------------------- 网关接线
class _FakeSettings:
    llm_provider = "openai"
    llm_model = "gpt-4o-mini"
    api_key = "sk-primary"
    llm_base_url = "http://localhost:3001/v1"


def _clear_provider_keys(monkeypatch) -> None:
    for key in ("OPENAI_API_KEY", "HERO_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
        monkeypatch.setenv(key, "")


def _specs(gw: LLMGateway) -> dict[str, ProviderSpec]:
    # 私有状态：from_settings 产出的链只有 _states 能拿到完整 spec（health()/snapshot() 故意不回显 base_url）
    return {st.spec.name: st.spec for st in gw._states}


def test_gateway_from_settings_passes_base_url_to_primary_only(monkeypatch):
    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")

    gw = LLMGateway.from_settings(_FakeSettings())
    specs = _specs(gw)

    assert specs["openai"].base_url == "http://localhost:3001/v1"
    # 回退 provider 不允许继承 primary 的网关地址（不同 provider 不串网关）
    assert specs["deepseek"].base_url is None


def test_gateway_from_settings_without_base_url_uses_official_endpoint(monkeypatch):
    class _NoBaseUrlSettings:
        llm_provider = "openai"
        llm_model = "gpt-4o-mini"
        api_key = "sk-primary"

    _clear_provider_keys(monkeypatch)

    gw = LLMGateway.from_settings(_NoBaseUrlSettings())

    assert _specs(gw)["openai"].base_url is None


def test_gateway_client_forwards_base_url_to_factory(monkeypatch):
    """settings → ProviderSpec → LLMFactory.create(base_url=...) 全链路透传。"""
    captured: dict = {}

    class _StubClient:
        def chat(self, prompt, timeout=None):  # noqa: ARG002 - 契约桩
            return "ok"

    def _fake_create(self, **kwargs):  # noqa: ARG001 - 只捕获 kwargs
        captured.update(kwargs)
        return _StubClient()

    from hero_quant.llm import factory as factory_mod

    monkeypatch.setattr(factory_mod.LLMFactory, "create", _fake_create)

    gw = LLMGateway(
        [ProviderSpec(name="openai", model="gpt-4o-mini", api_key="sk-x", base_url="http://localhost:3001/v1")],
        settings=object(),
    )
    gw._client(_specs(gw)["openai"])

    assert captured["base_url"] == "http://localhost:3001/v1"


# ---------------------------------------------------------------- 链A 创建点
def test_final_answer_rebuild_passes_base_url(monkeypatch):
    """`_final_answer` 重建分支（未绑定工具的干净 chat）同样从 Settings 取 base_url。"""
    import hero_quant.api.server as srv
    from hero_quant.llm import factory as factory_mod
    from hero_quant.llm.client import LLMClient

    captured: list[dict] = []

    class _Chat:
        def invoke(self, prompt):  # noqa: ARG002 - 契约桩
            return "summary"

    def _fake_create(self, **kwargs):  # noqa: ARG001 - 只捕获 kwargs
        captured.append(kwargs)
        return _Chat()

    monkeypatch.setenv("HERO_LLM_BASE_URL", "http://localhost:3001/v1")
    monkeypatch.setenv("HERO_API_KEY", "sk-test")
    monkeypatch.setattr(factory_mod.LLMFactory, "create", _fake_create)

    # _chat=None → 触发「重建未绑定工具的干净 LLMClient」分支
    out = srv._final_answer(
        LLMClient(None),
        "q",
        "narrative",
        [{"type": "tool_result", "tool": "market_data", "content": "{}"}],
    )

    assert out == "summary"
    assert captured[0]["base_url"] == "http://localhost:3001/v1"


def test_query_endpoint_passes_base_url_from_settings(monkeypatch):
    """/v1/query 链A 创建 client 时 base_url 来自 Settings（monkeypatch create 捕获 kwargs）。"""
    from fastapi.testclient import TestClient

    from hero_quant.api import security
    from hero_quant.llm import factory as factory_mod

    captured: list[dict] = []

    def _fake_create(self, **kwargs):  # noqa: ARG001 - 只捕获 kwargs
        captured.append(kwargs)
        raise RuntimeError("stop-after-capture")

    monkeypatch.setenv("HERO_LLM_BASE_URL", "http://localhost:3001/v1")
    monkeypatch.setenv("HERO_API_KEY", "sk-test")
    monkeypatch.setenv("HERO_ENV", "development")
    monkeypatch.setattr(factory_mod.LLMFactory, "create", _fake_create)

    from hero_quant.api.server import app

    ticket = security.issue_ticket(ttl=60, scope="query")
    TestClient(app).get("/v1/query", params={"q": "ping"}, headers={"X-Ticket": ticket})

    assert captured, "/v1/query 走了 _FakeLLM 兜底，链A 创建点未被覆盖"
    assert captured[0]["base_url"] == "http://localhost:3001/v1"
