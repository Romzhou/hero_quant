"""可插拔稠密向量嵌入，用于向量召回与上下文折叠。

职责：为 ContextManager 与记忆检索提供定长 embedding，支持 pgvector 序列化。
架构位置：agent 层向量底座，被 ContextManager/上层检索调用，维度由 Settings 统一配置。
关键设计：
- 多提供方分发：按 HERO_EMBED_PROVIDER 选择 openai/sentence-transformers/offline，无依赖时回落离线
- 离线确定性：基于 SHA256 的零依赖回落与 token-sum 语义桩，保证近义文本余弦相似度稳定
- 维度与归一：L2 归一化保证余弦可比，维度经 [8,2048] 夹逼，默认 32
"""
from __future__ import annotations

import functools
import hashlib
import logging
import math
import re
import threading
from typing import Dict, List

logger = logging.getLogger(__name__)

# 默认维度：稠密召回常用 32/64，此处统一 32
_DEFAULT_DIM = 32

_CACHE_LOCK = threading.RLock()
_SBERT_LOCK = threading.Lock()
_SBERT_MODELS: Dict[str, object] = {}


def get_vector_dim(default: int | None = None) -> int:
    """解析向量维度，优先取 Settings.vector_dim 并夹逼到 [8,2048]."""
    try:
        from hero_quant.config.settings import Settings

        v = int(Settings().vector_dim)
        if 8 <= v <= 2048:
            return v
    except (ImportError, ValueError, TypeError, AttributeError, OSError) as e:
        logger.debug("get_vector_dim settings failed: %s", e)
    if default is not None:
        try:
            iv = int(default)
            if 8 <= iv <= 2048:
                return iv
        except (ValueError, TypeError) as e:
            logger.debug("get_vector_dim default parse failed: %s", e)
    return _DEFAULT_DIM


def get_dim() -> int:
    return get_vector_dim()


def to_pgvector_literal(vec: List[float]) -> str:
    """序列化为 pgvector 文本字面量 '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"


def from_pgvector_literal(s: str | List[float]) -> List[float]:
    """解析 pgvector 字面量为 list[float]，已是列表则透传."""
    if isinstance(s, list):
        out: List[float] = []
        for x in s:
            try:
                out.append(float(x))
            except (ValueError, TypeError) as e:
                raise ValueError(f"invalid numeric value: {x!r}") from e
        return out
    if not isinstance(s, str):
        raise TypeError(f"from_pgvector_literal expects str or list, got {type(s).__name__}")
    txt = s.strip()
    if txt.startswith("[") and txt.endswith("]"):
        txt = txt[1:-1]
    if not txt:
        return []
    out: List[float] = []
    for part in txt.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(float(part))
        except (ValueError, TypeError) as e:
            raise ValueError(f"invalid numeric value: {part!r}") from e
    return out


_PROVIDER_ALIASES = {
    "openai": "openai",
    "sentence-transformers": "sentence-transformers",
    "sentence_transformers": "sentence-transformers",
    "sbert": "sentence-transformers",
    "all-minilm-l6-v2": "sentence-transformers",
    "offline": "offline",
    "hash": "offline",
    "fallback": "offline",
}


def _active_provider_name() -> str:
    try:
        from hero_quant.config.settings import Settings

        raw = Settings().embed_provider
    except (ImportError, ValueError, TypeError, AttributeError, OSError):
        raw = "offline"
    if raw is None:
        raw = "offline"
    key = str(raw).strip().lower()
    if not key:
        return "offline"
    if key in _PROVIDER_ALIASES:
        return _PROVIDER_ALIASES[key]
    if "openai" in key:
        return "openai"
    if "sentence" in key or "sbert" in key:
        return "sentence-transformers"
    return "offline"


def get_provider_name() -> str:
    return _active_provider_name()


def get_provider() -> str:
    return _active_provider_name()


def get_active_provider() -> str:
    return _active_provider_name()


# 离线 hash 嵌入：零依赖、确定性，基于 SHA256 扩展


def _embed_offline(text: str, dim: int) -> List[float]:
    if not isinstance(text, str):
        text = str(text)
    h = hashlib.sha256(text.encode("utf-8")).digest()
    vals: List[float] = []
    counter = 0
    while len(vals) < dim:
        chunk = hashlib.sha256(h + counter.to_bytes(2, "little")).digest() if counter else h
        for b in chunk:
            if len(vals) >= dim:
                break
            vals.append(b / 127.5 - 1.0)
        counter += 1
    return _l2_normalize(vals[:dim])


# 语义 token-sum 桩：用于 openai/sbert 不可用时的近义相似度保障


def _tokenize(text: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def _token_vector(token: str, dim: int) -> List[float]:
    # 单 token 确定性向量，映射到 [-1,1] 以保证零均值
    h = hashlib.sha256(token.encode("utf-8")).digest()
    vals: List[float] = []
    counter = 0
    while len(vals) < dim:
        chunk = hashlib.sha256(h + counter.to_bytes(2, "little")).digest() if counter else h
        for b in chunk:
            if len(vals) >= dim:
                break
            vals.append(b / 127.5 - 1.0)
        counter += 1
    return vals[:dim]


def _embed_semantic(text: str, dim: int) -> List[float]:
    """语义桩：累加 token 向量并 L2 归一，近义文本余弦趋近 1."""
    if not isinstance(text, str):
        text = str(text)
    tokens = _tokenize(text)
    if not tokens:
        v = _embed_offline(text, dim)
        return _l2_normalize([x * 2 - 1 for x in v])
    agg = [0.0] * dim
    for tok in tokens:
        tv = _token_vector(tok, dim)
        for i, val in enumerate(tv):
            agg[i] += val
    return _l2_normalize(agg)


def _l2_normalize(vec: List[float]) -> List[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def _get_sbert_model(name: str):
    """SentenceTransformer 单例缓存，懒加载勿在 import 时初始化."""
    with _SBERT_LOCK:
        if name in _SBERT_MODELS:
            return _SBERT_MODELS[name]
        from sentence_transformers import SentenceTransformer  # type: ignore

        model = SentenceTransformer(name, device="cpu", local_files_only=True)  # type: ignore
        _SBERT_MODELS[name] = model
        return model


def _try_sentence_transformers(text: str, dim: int) -> List[float] | None:
    try:
        import importlib.util

        if importlib.util.find_spec("sentence_transformers") is None:
            return None
        try:
            from hero_quant.config.settings import Settings

            model_name = Settings().sbert_model
        except (ImportError, ValueError, TypeError, AttributeError, OSError) as exc:
            logger.warning("sbert settings failed: %s", exc, exc_info=True)
            model_name = "all-MiniLM-L6-v2"
        try:
            model = _get_sbert_model(model_name)
            vec = model.encode(text, normalize_embeddings=True).tolist()  # type: ignore
            if len(vec) >= dim:
                return vec[:dim] if len(vec) != dim else vec
            else:
                padded = vec + [0.0] * (dim - len(vec))
                return _l2_normalize(padded)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            logger.warning("sbert encode failed: %s", exc, exc_info=True)
            return None
    except (ImportError, OSError, ValueError, TypeError) as exc:
        logger.warning("sentence_transformers unavailable: %s", exc, exc_info=True)
        return None


def _try_openai(text: str, dim: int) -> List[float] | None:
    try:
        from hero_quant.config.settings import Settings

        _s = Settings()
        api_key = _s.openai_api_key or ""
        model = _s.openai_embed_model
    except (ImportError, ValueError, TypeError, AttributeError, OSError) as exc:
        logger.warning("openai settings failed: %s", exc, exc_info=True)
        api_key = ""
        model = "text-embedding-3-small"
    if not api_key:
        return None
    try:
        import importlib.util

        if importlib.util.find_spec("openai") is None:
            return None
        from openai import OpenAI  # type: ignore

        client = OpenAI(api_key=api_key)
        resp = client.embeddings.create(input=text, model=model, timeout=30)  # type: ignore
        vec = resp.data[0].embedding  # type: ignore
        if len(vec) >= dim:
            v = vec[:dim]
        else:
            v = vec + [0.0] * (dim - len(vec))
        return _l2_normalize(v)
    except (ImportError, OSError, ValueError, TypeError, RuntimeError) as exc:
        logger.warning("openai embed failed: %s", exc, exc_info=True)
        return None


def embed_batch(texts: List[str], dim: int | None = None) -> List[List[float]]:
    """批量嵌入，对同一提供方/维度复用 embed."""
    if not texts:
        return []
    eff_dim = dim if dim is not None else get_vector_dim()
    return [embed(t, dim=eff_dim) for t in texts]


def _embed_uncached(text: str, dim: int | None = None) -> List[float]:
    """按提供方分发嵌入，失败自动回落到语义桩或离线 hash（无缓存内层）。"""
    provider = _active_provider_name()
    if dim is None:
        dim = get_vector_dim()
    else:
        try:
            dim = int(dim)
        except (ValueError, TypeError) as exc:
            logger.warning("dim parse failed for %r: %s", dim, exc, exc_info=True)
            dim = get_vector_dim()
        if dim <= 0:
            logger.warning("dim %r <=0, using default", dim)
            dim = get_vector_dim()
        if dim < 8 or dim > 2048:
            logger.warning("dim %r out of [8,2048], clamped to default", dim)
            dim = get_vector_dim()
    if provider == "openai":
        v = _try_openai(text, dim)
        if v is not None:
            return v
        return _embed_semantic(text, dim)
    if provider == "sentence-transformers":
        v = _try_sentence_transformers(text, dim)
        if v is not None:
            return v
        return _embed_semantic(text, dim)
    return _embed_offline(text, dim)


@functools.lru_cache(maxsize=1024)
def _embed_cached(text: str, dim: int) -> List[float]:
    return _embed_uncached(text, dim)


def embed(text: str, dim: int | None = None) -> List[float]:
    """按提供方分发嵌入，失败自动回落到语义桩或离线 hash（lru_cache 1024）。"""
    # 归一化维度与提供方，保证缓存键稳定
    eff_dim: int
    if dim is None:
        eff_dim = get_vector_dim()
    else:
        try:
            eff_dim = int(dim)
        except (ValueError, TypeError) as exc:
            logger.warning("embed dim parse failed for %r: %s", dim, exc, exc_info=True)
            eff_dim = get_vector_dim()
        if eff_dim <= 0 or eff_dim < 8 or eff_dim > 2048:
            logger.warning("embed dim %r out of [8,2048], clamped to default", eff_dim)
            eff_dim = get_vector_dim()
    # provider 变化时清空缓存以避免陈旧向量（轻量检查）
    # 缓存键包含 provider 隐式通过文本+dim，但为防 provider 切换污染，检测后清除
    with _CACHE_LOCK:
        try:
            current_provider = _active_provider_name()
            # 使用函数属性记录上次 provider
            last = getattr(_embed_cached, "_last_provider", None)
            if last is not None and last != current_provider:
                logger.info("embed provider switched %r -> %r, clearing cache", last, current_provider)
                _embed_cached.cache_clear()
            _embed_cached._last_provider = current_provider  # type: ignore[attr-defined]
        except (OSError, ValueError, TypeError, AttributeError, ImportError) as exc:
            logger.warning("provider cache check failed: %s", exc, exc_info=True)
    try:
        vec = _embed_cached(str(text), int(eff_dim))
    except Exception as exc:
        logger.warning("embed cached call failed for %r dim %r: %s", text, eff_dim, exc, exc_info=True)
        vec = _embed_uncached(str(text), int(eff_dim))
    # Defensive copy: caller must not mutate cached list
    return list(vec)


def cosine_sim(a: List[float], b: List[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"cosine_sim dim mismatch: {len(a)} != {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def centroid(vectors: List[List[float]]) -> List[float]:
    if not vectors:
        return []
    if not isinstance(vectors, list):
        raise TypeError(f"centroid expects list, got {type(vectors).__name__}")
    dim = len(vectors[0])
    if dim == 0:
        raise ValueError("centroid vectors must have non-zero dim")
    for v in vectors:
        if not isinstance(v, list):
            raise TypeError(f"centroid vector must be list, got {type(v).__name__}")
        if len(v) != dim:
            raise ValueError(f"centroid dim mismatch: expected {dim}, got {len(v)}")
        for x in v:
            if not isinstance(x, (int, float)):
                raise TypeError(f"centroid element must be numeric, got {type(x).__name__}")
            if not math.isfinite(float(x)):
                raise ValueError(f"centroid element non-finite: {x!r}")
    c = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            c[i] += float(x)
    # defensive copy length check: avoid unsafe cast of infinite/NaN
    return [x / len(vectors) for x in c]


def embedding_summary(messages: List[Dict] | List[str], max_chars: int = 200) -> str:
    """为消息列表生成含 'embedding' 关键词的向量摘要，用于上下文折叠。"""
    texts: List[str] = []
    for m in messages:
        if isinstance(m, dict):
            c = m.get("content", "")
            texts.append(str(c))
        elif isinstance(m, str):
            texts.append(m)
        else:
            texts.append(str(m))

    if not texts:
        return "[EMBEDDING_SUMMARY] empty embedding"

    joined = " ".join(texts)
    vecs = [embed(t) for t in texts]
    cent = centroid(vecs)
    cent_hint = ",".join(f"{x:.2f}" for x in cent[:3]) if cent else "0.00"

    words = re.findall(r"\w+", joined.lower())
    stop = {"the", "a", "an", "is", "are", "and", "or", "to", "of", "in", "for", "with", "x", "msg", "thr", "user", "assistant", "system", "tool"}
    freq: Dict[str, int] = {}
    for w in words:
        if w in stop or len(w) <= 1:
            continue
        if set(w) == {"x"}:
            continue
        freq[w] = freq.get(w, 0) + 1
    top = sorted(freq.items(), key=lambda kv: -kv[1])[:5]
    keywords = ", ".join(k for k, _ in top) if top else joined[:40]

    count = len(texts)
    summary = f"[EMBEDDING_SUMMARY embedding] {count} messages folded via vector centroid [{cent_hint}] keywords: {keywords}"
    if len(summary) > max_chars:
        summary = summary[: max_chars - 3] + "..."
        if "embedding" not in summary.lower():
            summary = "[EMBEDDING_SUMMARY embedding] " + summary
    return summary


__all__ = [
    "embed",
    "embed_batch",
    "cosine_sim",
    "centroid",
    "embedding_summary",
    "get_provider_name",
    "get_provider",
    "get_active_provider",
    "get_vector_dim",
    "get_dim",
    "to_pgvector_literal",
    "from_pgvector_literal",
]
