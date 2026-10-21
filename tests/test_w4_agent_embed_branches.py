"""W4 覆盖率补齐：agent/embed.py 未覆盖分支。

覆盖目标（src/hero_quant/agent/embed.py）：
- get_vector_dim：Settings 命中区间 / 越界回落 / 显式 default 生效 / default 非法回落
- to_pgvector_literal 与 from_pgvector_literal 的序列化/反序列化与非法输入
- _active_provider_name 的别名表与子串匹配、未知名回落 offline
- _embed_offline 的确定性与 L2 归一、_embed_semantic 的无 token 回落
- _l2_normalize 的零范数保护
- embed_batch 空输入、embed 的维度夹逼与返回副本语义
- cosine_sim 的维度不一致与零范数
- centroid 的空/非 list/零维/维度不一致/非数值/非有限 六类异常
- embedding_summary 的空输入与关键词抽取

断言均为具体数值/异常类型/字符串片段，不使用弱断言。
"""

from __future__ import annotations

import math

import pytest

from hero_quant.agent import embed as emb


# ---------------------------------------------------------------- 维度解析


def test_get_vector_dim_uses_settings_when_in_range(monkeypatch):
    """Settings.vector_dim 落在 [8,2048] 时直接采用。"""
    monkeypatch.setenv("HERO_VECTOR_DIM", "64")
    assert emb.get_vector_dim() == 64


def test_get_vector_dim_out_of_range_falls_back(monkeypatch):
    """Settings.vector_dim 越界时回落默认维度。"""
    monkeypatch.setenv("HERO_VECTOR_DIM", "99999")
    assert emb.get_vector_dim() == emb._DEFAULT_DIM
    monkeypatch.setenv("HERO_VECTOR_DIM", "2")
    assert emb.get_vector_dim() == emb._DEFAULT_DIM


def _broken_settings(monkeypatch):
    """让 Settings() 构造失败（模拟配置不可用）。"""

    def _boom(*_a, **_kw):
        raise ValueError("settings unavailable")

    monkeypatch.setattr("hero_quant.config.settings.Settings", _boom)


def test_get_vector_dim_explicit_default_used_when_settings_invalid(monkeypatch):
    """Settings 不可用时采用调用方给的 default。"""
    _broken_settings(monkeypatch)
    assert emb.get_vector_dim(default=128) == 128


def test_get_vector_dim_invalid_default_falls_back(monkeypatch):
    """default 非法时回落默认维度。"""
    _broken_settings(monkeypatch)
    assert emb.get_vector_dim(default="abc") == emb._DEFAULT_DIM


def test_get_dim_alias_matches_get_vector_dim():
    """get_dim 为 get_vector_dim 的别名。"""
    assert emb.get_dim() == emb.get_vector_dim()


# ---------------------------------------------------------------- pgvector 序列化


def test_to_pgvector_literal_format():
    """序列化为 6 位小数的 '[a,b,...]' 字面量。"""
    assert emb.to_pgvector_literal([1.0, -0.5]) == "[1.000000,-0.500000]"


def test_from_pgvector_literal_parses_string():
    """解析 pgvector 文本字面量。"""
    assert emb.from_pgvector_literal("[1.0, 2.5]") == [1.0, 2.5]
    assert emb.from_pgvector_literal("1.0,2.5") == [1.0, 2.5]


def test_from_pgvector_literal_empty_string():
    """空串返回空列表。"""
    assert emb.from_pgvector_literal("") == []
    assert emb.from_pgvector_literal("[]") == []


def test_from_pgvector_literal_passes_through_list():
    """已是列表时逐元素转 float。"""
    assert emb.from_pgvector_literal([1, 2]) == [1.0, 2.0]


def test_from_pgvector_literal_rejects_invalid_number():
    """非数值分量抛 ValueError。"""
    with pytest.raises(ValueError, match="invalid numeric value"):
        emb.from_pgvector_literal("[1.0, abc]")


def test_from_pgvector_literal_rejects_wrong_type():
    """非 str/list 输入抛 TypeError。"""
    with pytest.raises(TypeError, match="expects str or list"):
        emb.from_pgvector_literal(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------- provider 归一


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("openai", "openai"),
        ("sentence-transformers", "sentence-transformers"),
        ("sentence_transformers", "sentence-transformers"),
        ("sbert", "sentence-transformers"),
        ("all-minilm-l6-v2", "sentence-transformers"),
        ("offline", "offline"),
        ("hash", "offline"),
        ("fallback", "offline"),
        ("my-openai-proxy", "openai"),
        ("my-sentence-server", "sentence-transformers"),
        ("mystery", "offline"),
        ("", "offline"),
    ],
)
def test_active_provider_name_aliases(monkeypatch, raw, expected):
    """提供方别名/子串归一到三大实现，未知名回落 offline。"""
    monkeypatch.setenv("HERO_EMBED_PROVIDER", raw)
    assert emb._active_provider_name() == expected
    assert emb.get_provider_name() == expected
    assert emb.get_provider() == expected
    assert emb.get_active_provider() == expected


# ---------------------------------------------------------------- 嵌入计算


def test_embed_offline_is_deterministic_and_normalized():
    """离线嵌入确定性且 L2 归一。"""
    a = emb._embed_offline("hello world", 32)
    b = emb._embed_offline("hello world", 32)
    assert a == b
    assert len(a) == 32
    assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0, rel_tol=1e-9)


def test_embed_offline_accepts_non_string():
    """非字符串输入被转字符串处理。"""
    assert len(emb._embed_offline(12345, 16)) == 16


def test_embed_semantic_without_tokens_falls_back():
    """无 token 的文本回落为离线向量（长度正确）。"""
    vec = emb._embed_semantic("!!!", 16)
    assert len(vec) == 16
    assert math.isclose(math.sqrt(sum(x * x for x in vec)), 1.0, rel_tol=1e-9)


def test_embed_semantic_is_deterministic():
    """语义桩确定性。"""
    assert emb._embed_semantic("alpha beta gamma", 16) == emb._embed_semantic("alpha beta gamma", 16)


def test_l2_normalize_zero_norm_returns_input():
    """零范数向量原样返回（不除零）。"""
    assert emb._l2_normalize([0.0, 0.0]) == [0.0, 0.0]


def test_embed_batch_empty_returns_empty_list():
    """空输入返回空列表。"""
    assert emb.embed_batch([]) == []


def test_embed_batch_matches_single_embed():
    """批量嵌入与单条嵌入结果一致。"""
    texts = ["a", "b"]
    batch = emb.embed_batch(texts, dim=16)
    assert len(batch) == 2
    assert batch[0] == emb.embed("a", dim=16)


def test_embed_clamps_invalid_dim(monkeypatch):
    """非法维度被夹逼回默认（不抛）。"""
    monkeypatch.setenv("HERO_EMBED_PROVIDER", "offline")
    vec = emb.embed("x", dim=3)
    assert len(vec) == emb.get_vector_dim()
    vec2 = emb.embed("x", dim="abc")
    assert len(vec2) == emb.get_vector_dim()


def test_embed_returns_defensive_copy():
    """返回值为副本，调用方修改不影响缓存。"""
    a = emb.embed("copy-test", dim=16)
    a[0] = 12345.0
    b = emb.embed("copy-test", dim=16)
    assert b[0] != 12345.0


# ---------------------------------------------------------------- 相似度与质心


def test_cosine_sim_identical_is_one():
    """同向量余弦为 1。"""
    v = [0.3, 0.4]
    assert emb.cosine_sim(v, v) == pytest.approx(1.0, rel=1e-9)


def test_cosine_sim_orthogonal_is_zero():
    """正交向量余弦为 0。"""
    assert emb.cosine_sim([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0, abs=1e-12)


def test_cosine_sim_dim_mismatch_raises():
    """维度不一致抛 ValueError。"""
    with pytest.raises(ValueError, match="dim mismatch"):
        emb.cosine_sim([1.0, 0.0], [1.0, 0.0, 0.0])


def test_cosine_sim_zero_norm_returns_zero():
    """零范数向量余弦为 0（不除零）。"""
    assert emb.cosine_sim([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_centroid_empty_returns_empty_list():
    """空输入返回空列表。"""
    assert emb.centroid([]) == []


def test_centroid_computes_mean():
    """质心为逐维均值。"""
    assert emb.centroid([[1.0, 3.0], [3.0, 5.0]]) == [pytest.approx(2.0), pytest.approx(4.0)]


def test_centroid_non_list_raises_type_error():
    """非 list 输入抛 TypeError。"""
    with pytest.raises(TypeError, match="centroid expects list"):
        emb.centroid("nope")  # type: ignore[arg-type]


def test_centroid_zero_dim_raises():
    """零维向量抛 ValueError。"""
    with pytest.raises(ValueError, match="non-zero dim"):
        emb.centroid([[]])


def test_centroid_vector_not_list_raises():
    """成员非 list 抛 TypeError。"""
    with pytest.raises(TypeError, match="centroid vector must be list"):
        emb.centroid([(1.0, 2.0)])  # type: ignore[list-item]


def test_centroid_dim_mismatch_raises():
    """成员维度不一致抛 ValueError。"""
    with pytest.raises(ValueError, match="centroid dim mismatch"):
        emb.centroid([[1.0, 2.0], [1.0]])


def test_centroid_non_numeric_element_raises():
    """非数值分量抛 TypeError。"""
    with pytest.raises(TypeError, match="centroid element must be numeric"):
        emb.centroid([["a", "b"]])  # type: ignore[list-item]


def test_centroid_non_finite_element_raises():
    """非有限分量抛 ValueError。"""
    with pytest.raises(ValueError, match="non-finite"):
        emb.centroid([[float("nan"), 1.0]])


# ---------------------------------------------------------------- 摘要


def test_embedding_summary_empty_returns_placeholder():
    """空消息列表返回固定占位。"""
    assert emb.embedding_summary([]) == "[EMBEDDING_SUMMARY] empty embedding"


def test_embedding_summary_contains_keywords_and_count():
    """摘要含 embedding 关键词、消息条数与抽取出的高频词。"""
    msgs = [{"content": "alpha beta"}, {"content": "alpha gamma"}]
    out = emb.embedding_summary(msgs)
    assert "[EMBEDDING_SUMMARY embedding]" in out
    assert "2 messages folded" in out
    assert "alpha" in out


def test_embedding_summary_accepts_strings_and_others():
    """字符串/其他类型的消息均被接受。"""
    out = emb.embedding_summary(["alpha beta", 42])
    assert "2 messages folded" in out
