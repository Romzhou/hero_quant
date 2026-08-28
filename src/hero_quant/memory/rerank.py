"""Cohere reranker with fallback to local rank_fusion and prometheus fallback counter."""

from __future__ import annotations

import logging
import os
from typing import List, Tuple

logger = logging.getLogger("hero_quant.memory.rerank")

# prometheus counter or simple fallback
try:
    from prometheus_client import Counter, REGISTRY  # type: ignore

    try:
        rerank_fallback_total = Counter(
            "hero_quant_rerank_fallback_total",
            "Total Cohere rerank fallback events",
        )
    except Exception:
        # duplicate registration reuse
        try:
            rerank_fallback_total = REGISTRY._names_to_collectors.get("hero_quant_rerank_fallback_total")  # type: ignore
            if rerank_fallback_total is None:
                raise KeyError
        except Exception:
            # fallback simple counter
            class _SimpleCounter:
                def __init__(self):
                    self._value = 0

                def inc(self, amount=1):
                    self._value += amount

                def _get(self):
                    return self._value

            rerank_fallback_total = _SimpleCounter()  # type: ignore
except Exception:
    # prometheus not available
    class _SimpleCounter:
        def __init__(self):
            self._value = 0

        def inc(self, amount=1):
            self._value += amount

        def _get(self):
            return self._value

    rerank_fallback_total = _SimpleCounter()  # type: ignore

# simple integer mirror for tests that cannot read prometheus internals
_fallback_count: int = 0


def get_fallback_count() -> int:
    try:
        if hasattr(rerank_fallback_total, "_value"):
            v = getattr(rerank_fallback_total, "_value")
            # prometheus Counter stores _value as Wrapped
            try:
                return int(v.get()) if hasattr(v, "get") else int(v)
            except Exception:
                return int(_fallback_count)
        # check prometheus value
        if hasattr(rerank_fallback_total, "_metrics"):
            # sum all labeled values
            try:
                total = 0
                for m in getattr(rerank_fallback_total, "_metrics", {}).values():
                    total += getattr(m, "_value", 0) if hasattr(m, "_value") else 0
                    if hasattr(total, "get"):
                        total = total.get()
                return int(total)
            except Exception:
                pass
    except Exception:
        pass
    return int(_fallback_count)


def _inc_fallback() -> None:
    global _fallback_count
    _fallback_count += 1
    try:
        rerank_fallback_total.inc()  # type: ignore
    except Exception:
        pass


class CohereReranker:
    """Wraps POST https://api.cohere.ai/v1/rerank with httpx/timeout fallback."""

    def __init__(self, api_key: str | None = None, timeout: int = 5, model: str = "rerank-v3.5"):
        if api_key is not None:
            self.api_key = str(api_key).strip()
        else:
            try:
                from hero_quant.config.settings import Settings as _Settings

                self.api_key = (_Settings().cohere_api_key or "").strip()
            except Exception:
                self.api_key = ""
        self.timeout = int(timeout) if timeout else 5
        if self.timeout <= 0:
            self.timeout = 5
        self.model = model

    def rerank(self, query: str, candidates: List[Tuple[str, float]] | List[dict], top_k: int | None = None) -> List[Tuple[str, float]]:
        """Rerank candidates by Cohere relevance. Fallback to sorted candidates on failure.

        Args:
            query: query text
            candidates: list of (key, score) or dicts with key/score/content
        Returns:
            List of (key, score) sorted descending by rerank relevance (or fallback order)
        """
        # Normalize candidates to (key, score, text) where text used for rerank payload
        normalized: List[Tuple[str, float, str]] = []
        for item in (candidates or []):
            try:
                if isinstance(item, dict):
                    key = str(item.get("key") or item.get("id") or "")
                    sc = item.get("score", item.get("relevance_score", item.get("_score", 0.0)))
                    try:
                        sc_f = float(sc) if sc is not None else 0.0
                    except Exception:
                        sc_f = 0.0
                    text = item.get("content") or item.get("text") or key
                    normalized.append((key, sc_f, str(text)))
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    key = str(item[0])
                    try:
                        sc_f = float(item[1]) if item[1] is not None else 0.0
                    except Exception:
                        sc_f = 0.0
                    # if 3 elements, third is text
                    text = str(item[2]) if len(item) >= 3 else key
                    normalized.append((key, sc_f, text))
            except Exception:
                continue

        if not normalized:
            return []

        # If no api key, immediate fallback
        if not self.api_key:
            _inc_fallback()
            # fallback ordering: sorted by original score descending
            fallback = sorted([(k, s) for k, s, _t in normalized], key=lambda x: (-x[1], x[0]))
            return fallback

        # Build payload
        docs = [{"text": t} for _, _, t in normalized]
        payload = {
            "model": self.model,
            "query": query or "",
            "documents": docs,
            "top_n": top_k if top_k is not None else len(docs),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            # Try httpx first
            try:
                import httpx  # type: ignore

                resp = httpx.post(
                    "https://api.cohere.ai/v1/rerank",
                    json=payload,
                    headers=headers,
                    timeout=self.timeout,
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"Cohere rerank status {resp.status_code}: {resp.text[:200]}")
                data = resp.json()
            except ImportError:
                # fallback to urllib
                import json as _json
                import urllib.request
                import urllib.error

                req = urllib.request.Request(
                    "https://api.cohere.ai/v1/rerank",
                    data=_json.dumps(payload).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as r:  # type: ignore
                    status = getattr(r, "status", 200)
                    body = r.read().decode("utf-8")
                    if status != 200:
                        raise RuntimeError(f"Cohere rerank status {status}: {body[:200]}")
                    data = _json.loads(body)

            # Expect {"results": [{"index": int, "relevance_score": float}, ...]}
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list) or not results:
                raise RuntimeError("Cohere rerank empty results")

            # Map index -> relevance
            scored: List[Tuple[str, float]] = []
            for res in results:
                try:
                    idx = int(res.get("index"))
                    rel = float(res.get("relevance_score", 0.0))
                    if 0 <= idx < len(normalized):
                        key = normalized[idx][0]
                        scored.append((key, rel))
                except Exception:
                    continue
            if not scored:
                raise RuntimeError("no valid rerank entries")
            # If top_n < total, need to include missing? Cohere returns top_n sorted.
            # Return as ranked by relevance desc (already)
            scored.sort(key=lambda x: (-x[1], x[0]))
            return scored
        except Exception as exc:
            logger.warning("cohere rerank fallback", extra={"error": str(exc)})
            _inc_fallback()
            # fallback: try local rank_fusion semantics not applicable with single list;
            # return original sorted by score
            try:
                # if we have both bm25/vec semantics, we cannot reconstruct,
                # fallback to sorting by original score
                fallback = sorted([(k, s) for k, s, _t in normalized], key=lambda x: (-x[1], x[0]))
                return fallback
            except Exception:
                return [(k, s) for k, s, _t in normalized]
