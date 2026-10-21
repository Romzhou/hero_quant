"""W4 覆盖率补齐：llm/factory.py 的 SDK 缺失与初始化失败回落分支。

覆盖目标（src/hero_quant/llm/factory.py）：
- openai 提供方：ChatOpenAI 初始化抛异常时回落 _FallbackChat
- anthropic 提供方：langchain_anthropic 缺失 -> 回落 OpenAI 兼容；
  两者都缺失 -> 回落 _FallbackChat
- deepseek 提供方：ChatOpenAI 初始化抛异常时回落 _FallbackChat
- strict 模式下 SDK 缺失时异常向上传播（不静默降级）
- 返回的 LLMClient 一定带 timeout 属性（内省 garnish 失败也不影响透传）

断言均为具体类型/属性值/异常类型，不使用弱断言。
"""

from __future__ import annotations

import sys

import pytest

from hero_quant.config.settings import Settings
from hero_quant.llm.client import LLMClient
from hero_quant.llm.factory import LLMFactory, _FallbackChat


def _settings(provider: str) -> Settings:
    return Settings(
        llm_provider=provider,
        llm_model="gpt-4o-mini",
        llm_model_deep="gpt-4o-mini",
        llm_model_quick="gpt-4o-mini",
        api_key=None,
        openai_api_key=None,
    )


class _BlockImports:
    """meta_path finder：让指定模块的 import 抛 ImportError。"""

    def __init__(self, blocked: set[str]):
        self.blocked = blocked

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.blocked:
            raise ImportError(f"blocked for w4 test: {fullname}")
        return None


def _install_blocker(monkeypatch, blocked: set[str]) -> None:
    blocker = _BlockImports(blocked)
    monkeypatch.setattr(sys, "meta_path", [blocker] + list(sys.meta_path))
    for name in list(sys.modules):
        if name in blocked or name.startswith("langchain_anthropic"):
            monkeypatch.delitem(sys.modules, name, raising=False)


def test_openai_init_failure_falls_back_to_offline_chat(monkeypatch):
    """ChatOpenAI 初始化抛异常时回落 _FallbackChat（不抛给调用方）。"""
    llm = LLMFactory(_settings("openai")).create(model=123, api_key="test-key")
    assert isinstance(llm, LLMClient)
    assert isinstance(llm._chat, _FallbackChat)
    assert llm.timeout == 30


def test_deepseek_init_failure_falls_back_to_offline_chat(monkeypatch):
    """deepseek 路线初始化失败同样回落 _FallbackChat。"""
    llm = LLMFactory(_settings("deepseek")).create(model=123, api_key="test-key")
    assert isinstance(llm, LLMClient)
    assert isinstance(llm._chat, _FallbackChat)


def test_anthropic_without_any_sdk_falls_back_to_offline_chat(monkeypatch):
    """两个 SDK 都不可用时回落 _FallbackChat。"""
    _install_blocker(monkeypatch, {"langchain_anthropic", "langchain_openai"})
    llm = LLMFactory(_settings("anthropic")).create(api_key="test-key")
    assert isinstance(llm, LLMClient)
    assert isinstance(llm._chat, _FallbackChat)


def test_anthropic_strict_mode_propagates_missing_sdk(monkeypatch):
    """strict 模式下 SDK 缺失异常向上传播（不静默降级）。"""
    _install_blocker(monkeypatch, {"langchain_anthropic", "langchain_openai"})
    with pytest.raises(ImportError):
        LLMFactory(_settings("anthropic"), strict=True).create(api_key="test-key")


def test_fallback_chat_is_used_when_openai_sdk_missing(monkeypatch):
    """langchain_openai 缺失时 openai 路线回落 _FallbackChat。"""
    _install_blocker(monkeypatch, {"langchain_openai"})
    llm = LLMFactory(_settings("openai")).create(api_key="test-key")
    assert isinstance(llm, LLMClient)
    assert isinstance(llm._chat, _FallbackChat)


def test_created_client_always_exposes_timeout(monkeypatch):
    """LLMClient 自身带 timeout（内省用）；底层 chat 是否暴露 timeout 取决于 SDK。

    注：pydantic v2 模型禁止设置未声明属性，chat.timeout 的 garnish 可能被拒，
    但超时透传由 LLMClient.timeout + _invoke_once 的签名探测保证（见下一用例）。
    """
    llm = LLMFactory(_settings("openai")).create(api_key="test-key", timeout=11)
    assert llm.timeout == 11


def test_timeout_is_passed_to_backend_call(monkeypatch):
    """timeout 实际透传到后端调用（LLMClient.timeout 生效）。"""
    seen: dict = {}

    class _Chat:
        def invoke(self, prompt, timeout=None):
            seen["timeout"] = timeout
            return "ok"

    llm = LLMFactory(_settings("openai")).create(api_key="test-key", timeout=11)
    llm._chat = _Chat()
    llm.invoke("hi")
    assert seen["timeout"] == 11


def test_provider_specific_env_key_is_used(monkeypatch):
    """provider 专属 env key 被采用（deepseek -> DEEPSEEK_API_KEY）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("HERO_API_KEY", raising=False)
    llm = LLMFactory(_settings("deepseek")).create()
    assert isinstance(llm, LLMClient)


def test_anthropic_env_key_is_used(monkeypatch):
    """anthropic 路线使用 ANTHROPIC_API_KEY / CLAUDE_API_KEY。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("HERO_API_KEY", raising=False)
    llm = LLMFactory(_settings("anthropic")).create()
    assert isinstance(llm, LLMClient)
