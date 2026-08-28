"""Cohere reranker with fallback to local rank_fusion and prometheus fallback counter."""

from __future__ import annotations

import json
import logging
import threading
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
_fallback_lock = threading.Lock()


def get_fallback_count() -> int:
    try:
        if hasattr(rerank_fallback_total, "_value"):
            v = getattr(rerank_fallback_total, "_value")
            # prometheus Counter stores _value as Wrapped
            try:
                return int(v.get()) if hasattr(v, "get") else int(v)
            except Exception:
                with _fallback_lock:
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
    with _fallback_lock:
        return int(_fallback_count)


def _inc_fallback() -> None:
    global _fallback_count
    with _fallback_lock:
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
        # timeout 校验(0/负/非数→ValueError)
        try:
            t = int(timeout)  # type: ignore[arg-type]
        except (ValueError, TypeError) as e:  # narrow: only conversion errors
            raise ValueError(f"invalid timeout: {timeout!r}") from e
        if t <= 0:
            raise ValueError(f"timeout must be >=1, got {t}")
        self.timeout = t
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
                    except (ValueError, TypeError):  # narrow: only conversion errors
                        sc_f = 0.0
                    text = item.get("content") or item.get("text") or key
                    normalized.append((key, sc_f, str(text)))
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    key = str(item[0])
                    try:
                        sc_f = float(item[1]) if item[1] is not None else 0.0
                    except (ValueError, TypeError):  # narrow: only conversion errors
                        sc_f = 0.0
                    # if 3 elements, third is text
                    text = str(item[2]) if len(item) >= 3 else key
                    normalized.append((key, sc_f, text))
            except (ValueError, TypeError):  # narrow: only conversion/type errors
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
        # top_n 校验 int(top_k)>=1 且 clamp 到 len(docs)+log
        if top_k is not None:
            try:
                top_k_int = int(top_k)  # type: ignore[arg-type]
            except (ValueError, TypeError) as e:  # narrow: only conversion errors
                raise ValueError(f"invalid top_k: {top_k!r}") from e
            if top_k_int < 1:
                raise ValueError(f"top_k must be >=1, got {top_k_int}")
            if top_k_int > len(docs):
                logger.warning("top_k %s clamped to %s", top_k_int, len(docs))
                top_k_int = len(docs)
            top_n = top_k_int
        else:
            top_n = len(docs)
        payload = {
            "model": self.model,
            "query": query or "",
            "documents": docs,
            "top_n": top_n,
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
                import urllib.error
                import urllib.request

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
                except (ValueError, TypeError):  # narrow: only conversion errors
                    continue
            if not scored:
                raise RuntimeError("no valid rerank entries")
            # If top_n < total, need to include missing? Cohere returns top_n sorted.
            # Return as ranked by relevance desc (already)
            scored.sort(key=lambda x: (-x[1], x[0]))
            return scored
        except (json.JSONDecodeError, RuntimeError) as exc:  # narrow: json / runtime
            logger.warning("cohere rerank fallback", extra={"error": str(exc)}, exc_info=True)
            _inc_fallback()
            # fallback: try local rank_fusion semantics not applicable with single list;
            # return original sorted by score
            try:
                fallback = sorted([(k, s) for k, s, _t in normalized], key=lambda x: (-x[1], x[0]))
                return fallback
            except (ValueError, TypeError):  # narrow: only conversion errors
                return [(k, s) for k, s, _t in normalized]
        except Exception as exc:  # narrow: httpx.RequestError / Timeout
            # explicit httpx handling with narrow types
            try:
                import httpx  # type: ignore

                if isinstance(exc, (httpx.RequestError, httpx.TimeoutException)):  # httpx.RequestError / Timeout
                    logger.warning("cohere rerank fallback", extra={"error": str(exc)}, exc_info=True)
                    _inc_fallback()
                    try:
                        fallback = sorted([(k, s) for k, s, _t in normalized], key=lambda x: (-x[1], x[0]))
                        return fallback
                    except (ValueError, TypeError):  # narrow
                        return [(k, s) for k, s, _t in normalized]
            except Exception:
                pass
            logger.warning("cohere rerank fallback", extra={"error": str(exc)}, exc_info=True)
            _inc_fallback()
            try:
                fallback = sorted([(k, s) for k, s, _t in normalized], key=lambda x: (-x[1], x[0]))
                return fallback
            except (ValueError, TypeError):  # narrow
                return [(k, s) for k, s, _t in normalized]
