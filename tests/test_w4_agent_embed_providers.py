"""W4 覆盖率补齐：agent/embed.py 的多提供方分支（openai / sentence-transformers）。

覆盖目标（src/hero_quant/agent/embed.py）：
- _try_openai：Settings 失败回落、无 api_key 回落、find_spec 缺失回落、
  正常调用（向量长度 >= dim 与 < dim 两种补齐）、异常回落
- _try_sentence_transformers：find_spec 缺失回落、Settings 失败用默认模型名、
  正常调用（补齐分支）、encode 异常回落
- _embed_uncached：provider=openai / sentence-transformers 时的语义桩回落
- embed：provider 切换时清缓存

注：sentence-transformers 未安装，用假模块（带 __spec__）驱动；
openai 已安装但本机无有效 key/外网，测试以假模块替换其客户端。

断言均为具体数值/类型/异常类型/调用记录，不使用弱断言。
"""

from __future__ import annotations

import importlib.machinery
import math
import sys
import types

import pytest

from hero_quant.agent import embed as emb


def _install_fake_module(name: str, **attrs) -> types.ModuleType:
    """注册带 __spec__ 的假模块（使 importlib.util.find_spec 能命中）。"""
    mod = types.ModuleType(name)
    mod.__spec__ = importlib.machinery.ModuleSpec(name, None)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _remove_fake_module(name: str) -> None:
    sys.modules.pop(name, None)


# ---------------------------------------------------------------- openai


class _FakeEmbedding:
    def __init__(self, vec):
        self.embedding = vec


class _FakeResp:
    def __init__(self, vec):
        self.data = [_FakeEmbedding(vec)]


class _FakeOpenAI:
    def __init__(self, api_key=None, vec=None, boom=False):
        self.api_key = api_key
        self._vec = vec if vec is not None else [0.1] * 8
        self._boom = boom
        self.calls: list[dict] = []
        self.embeddings = self

    def create(self, input=None, model=None, timeout=None):
        self.calls.append({"input": input, "model": model, "timeout": timeout})
        if self._boom:
            raise RuntimeError("openai boom")
        return _FakeResp(self._vec)


def test_try_openai_returns_none_without_api_key(monkeypatch):
    """无 api_key 时直接回落（不发起网络调用）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("HERO_EMBED_PROVIDER", "offline")
    assert emb._try_openai("hello", 8) is None


def test_try_openai_returns_none_when_settings_fail(monkeypatch):
    """Settings 不可用时 api_key 回落为空 -> 返回 None。"""

    def _boom(*_a, **_kw):
        raise ValueError("settings down")

    monkeypatch.setattr("hero_quant.config.settings.Settings", _boom)
    assert emb._try_openai("hello", 8) is None


def test_try_openai_normalizes_long_vector(monkeypatch):
    """向量长度 >= dim 时按 dim 截断并 L2 归一。"""
    client = _FakeOpenAI(vec=[0.5] * 16)
    _install_fake_module("openai", OpenAI=lambda api_key=None: client)
    try:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        out = emb._try_openai("hello", 8)
        assert out is not None
        assert len(out) == 8
        assert math.isclose(math.sqrt(sum(x * x for x in out)), 1.0, rel_tol=1e-9)
        assert client.calls[0]["timeout"] == 30
    finally:
        _remove_fake_module("openai")


def test_try_openai_pads_short_vector(monkeypatch):
    """向量长度 < dim 时补零后归一。"""
    client = _FakeOpenAI(vec=[0.5, 0.5])
    _install_fake_module("openai", OpenAI=lambda api_key=None: client)
    try:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        out = emb._try_openai("hello", 8)
        assert out is not None
        assert len(out) == 8
    finally:
        _remove_fake_module("openai")


def test_try_openai_returns_none_on_error(monkeypatch):
    """客户端异常时回落 None（不抛）。"""
    client = _FakeOpenAI(boom=True)
    _install_fake_module("openai", OpenAI=lambda api_key=None: client)
    try:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert emb._try_openai("hello", 8) is None
    finally:
        _remove_fake_module("openai")


def test_embed_uncached_openai_provider_falls_back_to_semantic(monkeypatch):
    """provider=openai 且客户端不可用时回落语义桩（长度正确）。"""
    monkeypatch.setenv("HERO_EMBED_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    vec = emb._embed_uncached("hello world", 16)
    assert len(vec) == 16
    assert math.isclose(math.sqrt(sum(x * x for x in vec)), 1.0, rel_tol=1e-9)


# ---------------------------------------------------------------- sentence-transformers


class _FakeSTModel:
    def __init__(self, vec=None, boom=False):
        self._vec = vec if vec is not None else [0.25] * 8
        self._boom = boom
        self.calls: list[str] = []

    def encode(self, text, normalize_embeddings=False):
        self.calls.append(text)
        if self._boom:
            raise RuntimeError("sbert boom")
        return types.SimpleNamespace(tolist=lambda: list(self._vec))


def test_try_sentence_transformers_returns_none_when_missing(monkeypatch):
    """sentence-transformers 未安装时返回 None。"""
    assert emb._try_sentence_transformers("hello", 8) is None


def test_try_sentence_transformers_normalizes(monkeypatch):
    """假 sbert 模型正常返回：长度 >= dim 时按 dim 截断（保持后端向量语义）。"""
    model = _FakeSTModel(vec=[0.5] * 16)
    _install_fake_module(
        "sentence_transformers",
        SentenceTransformer=lambda name, device=None, local_files_only=True: model,
    )
    try:
        monkeypatch.setenv("HERO_EMBED_PROVIDER", "sentence-transformers")
        out = emb._try_sentence_transformers("hello", 8)
        assert out is not None
        assert len(out) == 8
        assert out == [pytest.approx(0.5)] * 8
        assert model.calls == ["hello"]
    finally:
        _remove_fake_module("sentence_transformers")
        emb._SBERT_MODELS.clear()


def test_try_sentence_transformers_pads_short_vector(monkeypatch):
    """假 sbert 返回短向量时补齐到 dim 并 L2 归一。"""
    model = _FakeSTModel(vec=[0.5, 0.5])
    _install_fake_module(
        "sentence_transformers",
        SentenceTransformer=lambda name, device=None, local_files_only=True: model,
    )
    try:
        monkeypatch.setenv("HERO_EMBED_PROVIDER", "sentence-transformers")
        out = emb._try_sentence_transformers("hello", 8)
        assert out is not None
        assert len(out) == 8
        assert math.isclose(math.sqrt(sum(x * x for x in out)), 1.0, rel_tol=1e-9)
    finally:
        _remove_fake_module("sentence_transformers")
        emb._SBERT_MODELS.clear()


def test_try_sentence_transformers_returns_none_on_encode_error(monkeypatch):
    """encode 异常时返回 None。"""
    model = _FakeSTModel(boom=True)
    _install_fake_module(
        "sentence_transformers",
        SentenceTransformer=lambda name, device=None, local_files_only=True: model,
    )
    try:
        monkeypatch.setenv("HERO_EMBED_PROVIDER", "sentence-transformers")
        assert emb._try_sentence_transformers("hello", 8) is None
    finally:
        _remove_fake_module("sentence_transformers")
        emb._SBERT_MODELS.clear()


def test_try_sentence_transformers_uses_default_model_name_on_settings_error(monkeypatch):
    """Settings 不可用时使用默认模型名 all-MiniLM-L6-v2。"""
    seen: list[str] = []
    model = _FakeSTModel(vec=[0.5] * 8)

    def _factory(name, device=None, local_files_only=True):
        seen.append(name)
        return model

    _install_fake_module("sentence_transformers", SentenceTransformer=_factory)

    def _boom(*_a, **_kw):
        raise ValueError("settings down")

    monkeypatch.setattr("hero_quant.config.settings.Settings", _boom)
    try:
        monkeypatch.setenv("HERO_EMBED_PROVIDER", "sentence-transformers")
        out = emb._try_sentence_transformers("hello", 8)
        assert out is not None
        assert seen == ["all-MiniLM-L6-v2"]
    finally:
        _remove_fake_module("sentence_transformers")
        emb._SBERT_MODELS.clear()


def test_embed_uncached_sentence_transformers_provider_falls_back(monkeypatch):
    """provider=sentence-transformers 且依赖缺失时回落语义桩。"""
    monkeypatch.setenv("HERO_EMBED_PROVIDER", "sentence-transformers")
    vec = emb._embed_uncached("hello world", 16)
    assert len(vec) == 16


# ---------------------------------------------------------------- provider 切换


def test_embed_clears_cache_when_provider_switches(monkeypatch):
    """provider 切换时清空 lru 缓存（防陈旧向量污染）。"""
    monkeypatch.setenv("HERO_EMBED_PROVIDER", "offline")
    emb.embed("switch-test", dim=16)
    emb._embed_cached._last_provider = "openai"  # type: ignore[attr-defined]
    emb.embed("switch-test", dim=16)
    assert emb._embed_cached._last_provider == "offline"  # type: ignore[attr-defined]
