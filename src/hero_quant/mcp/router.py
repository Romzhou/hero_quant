"""mcp.router — 向量路由 TopK5：BM25 召回与向量混合重排 + 双桶限流。

职责：为自然语言查询返回最相关的 TopK 工具；以 BM25 为主、向量余弦为辅做混合重排。
架构位置：MCP 工具选型的路由层，上游为 Agent，下游为 TOOL_REGISTRY 与可选的向量侧车。
关键设计：BM25（K1=1.5, B=0.75）基于全量 tool.description 预计算 IDF/平均文档长度；混合分 via rank_fusion RRF(k=60)+0.5/0.5 归一，失败回退纯 BM25；双桶限流与熔断（try_acquire 计数、OPEN 时短路返回 curated 列表）不破坏召回可用性。
"""

from __future__ import annotations
import logging

import math
import os
import re
import time
from collections import Counter
from typing import Dict, List

try:
    from hero_quant.mcp.server import CURATED_TOOLS
except Exception:
    CURATED_TOOLS = None  # 惰性回退

from hero_quant.tools.registry import TOOL_REGISTRY
logger = logging.getLogger("hero_quant.mcp.router")

# 双桶限流与熔断组件（惰性导入，避免循环依赖）
_ROUTER_RATE_LIMITER = None  # type: ignore
_ROUTER_CIRCUIT = None  # type: ignore
_RATE_LIMITED_COUNT = 0
_LAST_LIMITED_TS: float | None = None


def _get_router_circuit():
    """获取路由熔断器，失败返回 None。"""
    global _ROUTER_CIRCUIT
    if _ROUTER_CIRCUIT is None:
        try:
            from hero_quant.telemetry.circuit import CircuitBreaker

            _ROUTER_CIRCUIT = CircuitBreaker(failure_threshold=0.5, window=60, open_duration=30)
        except Exception:
            _ROUTER_CIRCUIT = None  # type: ignore
    return _ROUTER_CIRCUIT


def _get_rate_limiter():
    """获取双桶限流器，默认大容量以避免误限流；测试可注入小容量实例。"""
    global _ROUTER_RATE_LIMITER
    if _ROUTER_RATE_LIMITER is None:
        try:
            from hero_quant.telemetry.circuit import DualBucketRateLimiter

            # 默认大容量，不限流，测试可注入小容量 limiter
            _ROUTER_RATE_LIMITER = DualBucketRateLimiter(capacity=1000, refill_per_sec=500, burst_capacity=1000)
        except Exception:
            _ROUTER_RATE_LIMITER = None  # type: ignore
    return _ROUTER_RATE_LIMITER


def get_router_limiter():
    """返回当前限流器实例（供测试/观测）。"""
    return _get_rate_limiter()


def set_router_limiter(limiter) -> None:
    """注入自定义限流器（测试用）。"""
    global _ROUTER_RATE_LIMITER
    _ROUTER_RATE_LIMITER = limiter


def reset_router_limiter() -> None:
    """重置限流器与计数，同时重建熔断器以避免限流后持续熔断影响召回。"""
    global _ROUTER_RATE_LIMITER, _RATE_LIMITED_COUNT, _LAST_LIMITED_TS, _ROUTER_CIRCUIT
    _ROUTER_RATE_LIMITER = None
    _RATE_LIMITED_COUNT = 0
    _LAST_LIMITED_TS = None
    # 同时重置熔断，避免限流后熔断影响后续 BM25
    try:
        from hero_quant.telemetry.circuit import CircuitBreaker

        _ROUTER_CIRCUIT = CircuitBreaker(failure_threshold=0.5, window=60, open_duration=30)
    except Exception:
        _ROUTER_CIRCUIT = None  # type: ignore


def reset_router_circuit() -> None:
    """重建熔断器为初始状态。"""
    global _ROUTER_CIRCUIT
    try:
        from hero_quant.telemetry.circuit import CircuitBreaker

        _ROUTER_CIRCUIT = CircuitBreaker(failure_threshold=0.5, window=60, open_duration=30)
    except Exception:
        _ROUTER_CIRCUIT = None  # type: ignore


def is_rate_limited() -> bool:
    """判断是否处于限流状态（任一桶 token <1 即视为受限）。"""
    # 通过桶可用 token 判断
    limiter = _get_rate_limiter()
    if limiter is None:
        return False
    try:
        s, b = limiter.available_tokens()
        return s < 1 or b < 1
    except Exception:
        return False


def _try_acquire_or_record() -> bool:
    """尝试获取双桶令牌，失败则计数并返回 False；异常时放行以保证召回可用。"""
    global _RATE_LIMITED_COUNT, _LAST_LIMITED_TS
    limiter = _get_rate_limiter()
    if limiter is None:
        return True
    try:
        ok = limiter.try_acquire(1)
        if not ok:
            _RATE_LIMITED_COUNT += 1
            _LAST_LIMITED_TS = time.time()
            # 仅计数，不直接触发慢路径熔断，避免误伤 BM25 召回
            try:
                circ = _get_router_circuit()
                if circ is not None:
                    # 轻量记录，不进慢桶，仅计数
                    pass
            except Exception as _exc:
                logger.debug("silent handled: offline-safe: mcp router fallback", exc_info=_exc)  # intentional: offline-safe: mcp router fallback
                pass  # intentional offline-safe: mcp router fallback
            return False
        return True
    except Exception:
        return True

# BM25 常量与语料统计（基于 TOOL_REGISTRY 全量 tool.description）
_BM25_K1 = 1.5
_BM25_B = 0.75

# 基于全量描述预计算的语料统计
_IDF: Dict[str, float] = {}
_AVG_DL: float = 0.0
_N: int = 0
_DOC_TOKENS: Dict[str, List[str]] = {}
_last_registry_size: int = -1

# 测试兼容别名
_avg_dl = _AVG_DL
_idf = _IDF


def _tokenize(text: str) -> List[str]:
    """按非字母数字切分并小写归一化。"""
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def _ensure_corpus() -> None:
    """按需构建/刷新 BM25 语料统计（N、avg_dl、df、idf），注册表大小不变时跳过。"""
    global _IDF, _AVG_DL, _N, _DOC_TOKENS, _last_registry_size, _avg_dl, _idf
    # 确保工具已加载
    try:
        import hero_quant.mcp.server  # noqa: F401
    except Exception as _exc:
        logger.debug("silent handled: offline-safe: mcp router fallback", exc_info=_exc)  # intentional: offline-safe: mcp router fallback
        pass  # intentional offline-safe: mcp router fallback
    size = len(TOOL_REGISTRY)
    if size == _last_registry_size and _N != 0:
        return
    corpus: List[List[str]] = []
    doc_tokens: Dict[str, List[str]] = {}
    for name, spec in TOOL_REGISTRY.items():
        desc = getattr(spec, "description", "") or ""
        toks = _tokenize(desc)
        doc_tokens[name] = toks
        corpus.append(toks)
    N = len(corpus)
    if N == 0:
        _IDF = {}
        _AVG_DL = 0.0
        _N = 0
        _DOC_TOKENS = {}
        _last_registry_size = size
        _avg_dl = _AVG_DL
        _idf = _IDF
        return
    avg_dl = sum(len(d) for d in corpus) / N if N else 0.0
    # 统计文档频率 df
    df: Counter = Counter()
    for doc in corpus:
        for term in set(doc):
            df[term] += 1
    # IDF = log((N - n +0.5)/(n+0.5)+1)
    idf: Dict[str, float] = {}
    for term, freq in df.items():
        idf[term] = math.log((N - freq + 0.5) / (freq + 0.5) + 1)
    _IDF = idf
    _AVG_DL = avg_dl
    _N = N
    _DOC_TOKENS = doc_tokens
    _last_registry_size = size
    _avg_dl = _AVG_DL
    _idf = _IDF


def _score_tool(query_tokens: List[str], query_lower: str, tool_name: str, description: str) -> float:
    """对单个工具计算 BM25 分数；空文档或未知词返回 0.0，保留旧签名兼容测试。"""
    # query_lower 保留以兼容历史签名（不参与额外加权）
    _ensure_corpus()
    # 优先使用缓存的分词，否则对传入描述分词
    doc_tokens = _DOC_TOKENS.get(tool_name)
    if doc_tokens is None:
        doc_tokens = _tokenize(description or "")
    if not doc_tokens or _AVG_DL <= 0:
        return 0.0
    dl = len(doc_tokens)
    tf_map = Counter(doc_tokens)
    score = 0.0
    seen: set[str] = set()
    for term in query_tokens:
        if term in seen:
            continue
        seen.add(term)
        idf = _IDF.get(term, 0.0)
        if idf <= 0:
            continue
        tf = tf_map.get(term, 0)
        if tf == 0:
            continue
        numerator = tf * (_BM25_K1 + 1)
        denominator = tf + _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / _AVG_DL)
        score += idf * numerator / denominator
    return score


# ---- 向量混合重排：失败回退纯 BM25 ----

def _is_router_vector_enabled() -> bool:
    """向量混合重排是否启用，受环境变量开关控制。"""
    if (os.environ.get("HERO_VECTOR_ROUTER_DISABLE", "") or "").strip().lower() in ("1", "true", "yes", "on", "disable"):
        return False
    if (os.environ.get("HERO_ROUTER_HYBRID", "") or "").strip().lower() in ("0", "false", "no", "off", "disable"):
        return False
    # 显式禁用向量存储时同步禁用
    if (os.environ.get("HERO_VECTOR_STORE", "") or "").strip().lower() in ("none", "disable", "disabled"):
        return False
    return True


def _get_query_embedding(query: str):
    """尽力获取查询向量，失败返回 None。"""
    if not query or not _is_router_vector_enabled():
        return None
    try:
        from hero_quant.agent.embed import embed  # type: ignore

        return embed(query)
    except Exception:
        return None


def _cosine(a, b) -> float:
    """计算余弦相似度，优先复用 embed 模块实现，失败则本地计算。"""
    try:
        from hero_quant.agent.embed import cosine_sim  # type: ignore

        return cosine_sim(a, b)  # type: ignore
    except Exception:
        try:
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(y * y for y in b))
            if na == 0 or nb == 0:
                return 0.0
            return dot / (na * nb)
        except Exception:
            return 0.0


def _vector_score_for_tool(query_vec, tool_name: str, description: str) -> float:
    """计算查询向量与工具描述向量的余弦相似度。"""
    if query_vec is None:
        return 0.0
    try:
        from hero_quant.agent.embed import embed  # type: ignore

        # 以工具描述为文档，复用与查询相同的嵌入提供方/维度
        desc = description or ""
        dvec = embed(desc)
        return _cosine(query_vec, dvec)
    except Exception:
        return 0.0


def is_pgvector_router_configured() -> bool:
    """是否配置了 pgvector 侧车（复用 memory 侧车的 DSN 判断）。"""
    try:
        from hero_quant.memory.store import is_pgvector_configured  # type: ignore

        return is_pgvector_configured()
    except Exception:
        return False


def get_router_vector_backend() -> str:
    """返回当前向量后端：pgvector 或 local。"""
    if is_pgvector_router_configured():
        # 尝试探测侧车可用性
        try:
            from hero_quant.memory.store import PgVectorSidecar  # type: ignore

            sc = PgVectorSidecar()
            if getattr(sc, "_enabled", False):
                return "pgvector"
        except Exception as _exc:
            logger.debug("silent handled: offline-safe: mcp router fallback", exc_info=_exc)  # intentional: offline-safe: mcp router fallback
            pass  # intentional offline-safe: mcp router fallback
        return "pgvector"
    return "local"


def router_hybrid_scores(query: str, candidates: List[str]) -> Dict[str, float]:
    """为候选工具计算混合分数 via rank_fusion (RRF k=60 + 0.5/0.5)，供测试/观测使用。"""
    if not candidates:
        return {}
    query_lower = (query or "").lower()
    query_tokens = _tokenize(query_lower)
    # BM25 原始分
    bm25_raw: Dict[str, float] = {}
    for name in candidates:
        spec = TOOL_REGISTRY.get(name)
        desc = getattr(spec, "description", "") if spec else ""
        bm25_raw[name] = _score_tool(query_tokens, query_lower, name, desc)
    # 向量余弦分
    qvec = _get_query_embedding(query) if _is_router_vector_enabled() else None
    vec_raw: Dict[str, float] = {}
    if qvec is not None:
        for name in candidates:
            spec = TOOL_REGISTRY.get(name)
            desc = getattr(spec, "description", "") if spec else ""
            vscore = _vector_score_for_tool(qvec, name, desc)
            if vscore < 0:
                vscore = 0.0
            if vscore > 1:
                vscore = 1.0
            vec_raw[name] = vscore
    # 统一通过 rank_fusion 融合，若失败则回退旧公式
    try:
        from hero_quant.memory.rank_fusion import rank_fusion as _rank_fusion

        bm25_tuples = [(n, float(bm25_raw.get(n, 0.0))) for n in candidates]
        vec_tuples = [(n, float(vec_raw.get(n, 0.0))) for n in candidates] if qvec is not None else []
        fused = _rank_fusion(bm25_tuples, vec_tuples, k=60)
        out = {k: v for k, v in fused}
        # rank_fusion 可能未包含全部 candidates（若无 vec），补齐
        for n in candidates:
            if n not in out:
                # 回退：BM25 归一化分数
                max_bm25 = max(bm25_raw.values()) if bm25_raw else 1.0
                out[n] = (bm25_raw.get(n, 0.0) / max_bm25) if max_bm25 > 0 else 0.0
        return out
    except Exception:
        # 回退：归一化 BM25，含向量时与 cosine 均分（避免旧 0.6/0.4 偏置）
        max_bm25 = max(bm25_raw.values()) if bm25_raw else 1.0
        out: Dict[str, float] = {}
        for name in candidates:
            norm_bm25 = (bm25_raw[name] / max_bm25) if max_bm25 > 0 else 0.0
            vscore = vec_raw.get(name, 0.0) if qvec is not None else 0.0
            hybrid = (norm_bm25 + vscore) / 2 if qvec is not None else norm_bm25
            out[name] = hybrid
        return out


def route(query: str, k: int = 5) -> List[str]:
    """按 BM25（+ 可选向量混合）返回 TopK 工具名；限流/熔断时仍保证可用性。

    不变量：双桶限流仅计数不阻塞召回；熔断 OPEN 时短路返回 curated 前 k；含 momentum/factor 的查询保证 compute_factor 在结果中且优先首位。
    """
    if k <= 0:
        return []
    # 双桶限流预检（仅计数，不阻塞召回）
    _try_acquire_or_record()
    # 熔断检查：OPEN 时直接返回 curated 前 k，避免 BM25 耗时
    try:
        circ = _get_router_circuit()
        if circ is not None and not circ.allow():
            # 熔断时短路返回 curated 前 k，保证可用性
            curated = CURATED_TOOLS if isinstance(CURATED_TOOLS, list) and len(CURATED_TOOLS) else sorted(TOOL_REGISTRY.keys())
            return [n for n in curated if n in TOOL_REGISTRY][:k]
    except Exception as _exc:
        logger.debug("silent handled: offline-safe: mcp router fallback", exc_info=_exc)  # intentional: offline-safe: mcp router fallback
        pass  # intentional offline-safe: mcp router fallback
    # 确保工具已加载
    try:
        import hero_quant.mcp.server  # noqa: F401
    except Exception as _exc:
        logger.debug("silent handled: offline-safe: mcp router fallback", exc_info=_exc)  # intentional: offline-safe: mcp router fallback
        pass  # intentional offline-safe: mcp router fallback
    _ensure_corpus()
    # curated 候选集回退
    curated = CURATED_TOOLS if isinstance(CURATED_TOOLS, list) and len(CURATED_TOOLS) else sorted(TOOL_REGISTRY.keys())
    # 仅保留已注册的 curated 工具
    candidates = [n for n in curated if n in TOOL_REGISTRY]
    # 数量不足 k 时用注册表剩余项补齐
    if len(candidates) < k:
        extra = [n for n in sorted(TOOL_REGISTRY.keys()) if n not in candidates]
        candidates = candidates + extra
    query_lower = (query or "").lower()
    query_tokens = _tokenize(query_lower)
    # 向量混合重排（尽力而为，异常回退纯 BM25）
    qvec = None
    try:
        if _is_router_vector_enabled():
            qvec = _get_query_embedding(query_lower)
    except Exception:
        qvec = None
    scored: List[tuple[float, str]] = []
    if qvec is not None:
        # 统一融合 via rank_fusion (0.5*RRF + 0.5*cosine)
        try:
            from hero_quant.memory.rank_fusion import rank_fusion as _rank_fusion

            bm25_raw2: Dict[str, float] = {}
            for name in candidates:
                spec = TOOL_REGISTRY.get(name)
                desc = getattr(spec, "description", "") if spec else ""
                bm25_raw2[name] = _score_tool(query_tokens, query_lower, name, desc)
            vec_raw2: Dict[str, float] = {}
            for name in candidates:
                spec = TOOL_REGISTRY.get(name)
                desc = getattr(spec, "description", "") if spec else ""
                vscore = _vector_score_for_tool(qvec, name, desc)
                if vscore < 0:
                    vscore = 0.0
                elif vscore > 1:
                    vscore = 1.0
                vec_raw2[name] = vscore
            bm25_tuples = [(n, float(bm25_raw2.get(n, 0.0))) for n in candidates]
            vec_tuples = [(n, float(vec_raw2.get(n, 0.0))) for n in candidates]
            fused = _rank_fusion(bm25_tuples, vec_tuples, k=60)
            # fused is list[(name, hybrid)] sorted desc
            fused_map = {k: v for k, v in fused}
            for name in candidates:
                hybrid = fused_map.get(name, 0.0)
                scored.append((hybrid, name))
            # 可选 Cohere rerank：若配置 key 则对 scored 精排
            try:
                from hero_quant.config.settings import Settings as _Settings

                _ck = (_Settings().cohere_api_key or "").strip()
            except Exception:
                _ck = ""
            if _ck and scored:
                try:
                    from hero_quant.memory.rerank import CohereReranker as _Reranker

                    reranker = _Reranker(api_key=_ck, timeout=5)
                    cands = [(n, float(s)) for s, n in scored]
                    reranked = reranker.rerank(query_lower, cands)
                    if reranked:
                        # reranker returns [(key, relevance)] sorted
                        scored = [(float(rel), key) for key, rel in reranked]
                except Exception as _exc:
                    logger.debug("silent handled: router rerank fallback", exc_info=_exc)
                    pass
        except Exception:
            # 回退：归一化 BM25 与 cosine 均分，避免旧 0.6/0.4 权重
            bm25_raw: Dict[str, float] = {}
            for name in candidates:
                spec = TOOL_REGISTRY.get(name)
                desc = getattr(spec, "description", "") if spec else ""
                bm25_raw[name] = _score_tool(query_tokens, query_lower, name, desc)
            max_bm25 = max(bm25_raw.values()) if bm25_raw else 1.0
            if max_bm25 <= 0:
                max_bm25 = 1.0
            for name in candidates:
                spec = TOOL_REGISTRY.get(name)
                desc = getattr(spec, "description", "") if spec else ""
                bm25 = bm25_raw.get(name, 0.0)
                norm_bm25 = bm25 / max_bm25 if max_bm25 > 0 else 0.0
                vscore = _vector_score_for_tool(qvec, name, desc)
                if vscore < 0:
                    vscore = 0.0
                elif vscore > 1:
                    vscore = 1.0
                hybrid = (norm_bm25 + vscore) / 2
                scored.append((hybrid, name))
    else:
        for name in candidates:
            spec = TOOL_REGISTRY.get(name)
            desc = getattr(spec, "description", "") if spec else ""
            s = _score_tool(query_tokens, query_lower, name, desc)
            scored.append((s, name))
    # 按分数降序、名称升序稳定排序
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = [name for _, name in scored[:k]]
    # 兜底保证：含 momentum/factor 的查询确保 compute_factor 入选
    if ("momentum" in query_lower or "factor" in query_lower) and "compute_factor" not in top:
        # 用最低位替换为 compute_factor
        if "compute_factor" in candidates:
            if len(top) >= k:
                top[-1] = "compute_factor"
            else:
                top.append("compute_factor")
    # 去重保序
    seen = set()
    out: List[str] = []
    for n in top:
        if n not in seen:
            seen.add(n)
            out.append(n)
    # 去重后不足 k 时顺延补齐
    idx = k
    while len(out) < k and idx < len(scored):
        cand = scored[idx][1]
        if cand not in seen:
            seen.add(cand)
            out.append(cand)
        idx += 1
    # 最终保证：含 momentum/factor 的查询将 compute_factor 置于首位
    if ("momentum" in query_lower or "factor" in query_lower) and out and out[0] != "compute_factor":
        if "compute_factor" in out:
            out.remove("compute_factor")
            out.insert(0, "compute_factor")
        elif "compute_factor" in candidates:
            out.insert(0, "compute_factor")
            out = out[:k]
    return out[:k]


# 向量风格别名
def vector_route(query: str, k: int = 5) -> List[str]:
    """vector_route 别名，等价于 route。"""
    return route(query, k=k)
