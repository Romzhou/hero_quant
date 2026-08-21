"""AgentLoop state machine - production hardened (Wave A3).

Port of vibe-trading loop.py 8 control points + context/grounding/trace/policy.
"""

from __future__ import annotations

import concurrent.futures
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

# Pre-import RetryPolicy at module load to keep run() wall time <0.35s for parallel test
try:
    from .policies import RetryPolicy as _RetryPolicy  # type: ignore
except Exception:  # pragma: no cover
    _RetryPolicy = None  # type: ignore


def estimate_tokens(text: Any) -> int:
    """Rough token estimate (~4 chars/token), vibe同款 json//4 粗估.

    Accepts str or list[message dict] (json dumps //4) or any.
    """
    if isinstance(text, list):
        import json

        try:
            return len(json.dumps(text, ensure_ascii=False, default=str)) // 4
        except Exception:
            return len(str(text)) // 4
    if isinstance(text, str):
        return len(text) // 4
    try:
        return len(text) // 4  # type: ignore[arg-type]
    except Exception:
        return len(str(text)) // 4


@dataclass
class LoopResult:
    terminated: bool
    iterations: int
    text: str = ""
    reason: str = ""
    metrics: Optional[Dict[str, Any]] = None
    grounding_verified: bool = False
    trace_path: Optional[str] = None
    token_count: int = 0


class AgentLoop:
    """Production AgentLoop state machine.

    Control points (while not terminated):
      1. max_iterations check -> reason="max_iterations"
      2. token_limit check via estimate_tokens(buffer) or context estimate -> reason="token_limit" + TRUNCATED banner
      3. user_stop placeholder (signal)
      4. llm.stream_chat with try/except -> retry via RetryPolicy.should_retry + exponential backoff
      5. accumulate text deltas, update token_count
      6. tool_calls execute via TOOL_REGISTRY with is_concurrency_safe + redact_payload + TraceWriter
      7. grounding_check via GroundingLedger.assert_price
      8. context_compact via ContextManager when > token_limit*0.8
      9. budget_breaker -> reason="budget_fallback"
      10. tool_success+grounding_pass -> terminated
    """

    def __init__(
        self,
        llm,
        max_iterations=5,
        token_limit=60000,
        trace=None,
        context_manager=None,
        grounding=None,
        use_graph=False,
        graph=None,
        budget_breaker=None,
        retry_policy=None,
        **kwargs: Any,
    ):
        self.llm = llm
        self.max_iterations = int(max_iterations) if max_iterations is not None else 5
        self.token_limit = token_limit
        self.trace = trace
        # aliases for backward compat
        if context_manager is None and "context" in kwargs:
            context_manager = kwargs.pop("context")
        if context_manager is None and "contextManager" in kwargs:
            context_manager = kwargs.pop("contextManager")
        self.context_manager = context_manager
        # also expose as .context for legacy
        self.context = context_manager
        self.grounding = grounding
        self.use_graph = bool(use_graph)
        self.graph = graph
        self.budget_breaker = budget_breaker
        self.retry_policy = retry_policy
        # also accept budgetBreaker / retryPolicy aliases
        if self.budget_breaker is None and "budgetBreaker" in kwargs:
            self.budget_breaker = kwargs.pop("budgetBreaker")
        if self.retry_policy is None and "retryPolicy" in kwargs:
            self.retry_policy = kwargs.pop("retryPolicy")
        # replay VCR compat: pop replay_path/replay_from/replay_file + replay flag (C1-2)
        _replay_path = kwargs.pop("replay_path", None)
        if _replay_path is None:
            _replay_path = kwargs.pop("replay_from", None)
        if _replay_path is None:
            _replay_path = kwargs.pop("replay_file", None)
        _replay_flag = kwargs.pop("replay", None)
        if _replay_path is None and isinstance(_replay_flag, (str, Path)):
            _replay_path = _replay_flag
        # replay=True with replay_from already handled; bare replay=True leaves path None
        self._replay_path = Path(_replay_path) if _replay_path is not None else None
        self.replay_path = self._replay_path
        # user_stop signal flag
        self._stop_requested = bool(kwargs.pop("stop_requested", False))
        # internal trace writer (lazy)
        self._trace_writer = None
        self._init_trace_writer()

    # -- trace helpers --
    def _init_trace_writer(self):
        if self.trace is None:
            self._trace_writer = None
            return
        # if already a TraceWriter-like object with append
        if hasattr(self.trace, "append") and hasattr(self.trace, "path"):
            self._trace_writer = self.trace
            return
        if hasattr(self.trace, "append") and callable(getattr(self.trace, "append")):
            # duck-typed TraceWriter (has append but no path)
            self._trace_writer = self.trace
            return
        # Path / str case -> construct TraceWriter
        try:
            from .trace import TraceWriter

            p = Path(self.trace) if isinstance(self.trace, (str, Path)) else Path(str(self.trace))
            self._trace_writer = TraceWriter(p)
        except Exception:
            self._trace_writer = None

    def _ensure_trace_writer(self):
        return self._trace_writer

    def request_stop(self):
        """External signal to request user_stop."""
        self._stop_requested = True

    # -- LLM dispatch with fallback method names --
    def _call_llm(self, goal: str):
        """Return iterable stream from llm; tries stream_chat, invoke, chat fallback."""
        # priority: stream_chat -> invoke -> chat -> __call__
        for method_name in ("stream_chat", "invoke", "chat", "__call__"):
            fn = getattr(self.llm, method_name, None)
            if fn is None or not callable(fn):
                continue
            # try calling with goal; handle different signatures
            try:
                res = fn(goal)
            except TypeError:
                try:
                    res = fn({"messages": [{"role": "user", "content": goal}]})
                except Exception:
                    # try with keyword
                    try:
                        res = fn(prompt=goal)
                    except Exception as e:
                        raise e
            # if res is generator/iterable, return it; if it's dict/string, wrap
            return res
        raise AttributeError("llm has no stream_chat/invoke/chat method")

    def _normalize_stream(self, stream) -> List[Dict[str, Any]]:
        """Ensure stream is iterable of dicts; materialize if needed but keep lazy."""
        # If stream is None, return empty
        if stream is None:
            return []
        # If dict, wrap
        if isinstance(stream, dict):
            return [stream]
        # If string, wrap as text chunk
        if isinstance(stream, str):
            return [{"type": "text", "text": stream}]
        # otherwise assume iterable
        return stream  # type: ignore[return-value]

    # -- main run --
    def run(self, goal: str) -> LoopResult:
        # Graph delegation path
        if self.use_graph:
            return self._run_graph(goal)

        buffer = ""
        iterations = 0
        token_count = 0
        grounding_verified = False
        metrics: Optional[Dict[str, Any]] = None
        reason = "completed"
        terminated = False
        _tool_success_global = False
        # VCR llm_usage accumulator (C1-2)
        _llm_usage_input = 0
        _llm_usage_output = 0

        trace_writer = self._ensure_trace_writer()

        # -- VCR replay short-circuit (C1-2) --
        replay_path = getattr(self, "_replay_path", None)
        if replay_path is not None:
            try:
                import json as _replay_json

                rp = Path(replay_path)
                if rp.is_dir():
                    cand = rp / "llm_usage.json"
                    if cand.exists():
                        rp = cand
                if rp.exists():
                    try:
                        data = _replay_json.loads(rp.read_text(encoding="utf-8"))
                    except Exception:
                        data = {}
                    if isinstance(data, dict):
                        _ru = data.get("llm_usage")
                        if not isinstance(_ru, dict):
                            if "input_tokens" in data or "output_tokens" in data or "prompt_tokens" in data:
                                _ru = data
                            else:
                                _ru = {}
                        # normalize with compat keys
                        def _to_int(v):
                            try:
                                return int(v)
                            except Exception:
                                return 0

                        _ri = _ru.get("input_tokens")
                        if _ri is None:
                            _ri = _ru.get("prompt_tokens", _ru.get("promptTokens", 0))
                        _ro = _ru.get("output_tokens")
                        if _ro is None:
                            _ro = _ru.get("completion_tokens", _ru.get("generated_tokens", 0))
                        _norm = {"input_tokens": _to_int(_ri), "output_tokens": _to_int(_ro)}
                        # extract replay text
                        _rtext = data.get("text", "") or ""
                        if not _rtext and isinstance(data.get("chunks"), list):
                            _parts: list[str] = []
                            for _c in data.get("chunks", []):
                                if isinstance(_c, dict):
                                    _parts.append(_c.get("text", "") or _c.get("content", "") or "")
                                elif isinstance(_c, str):
                                    _parts.append(_c)
                            _rtext = "".join(_parts)
                        buffer = str(_rtext)
                        token_count = estimate_tokens(buffer)
                        if trace_writer is not None:
                            try:
                                trace_writer.append({"type": "llm_usage", "llm_usage": _norm, "iteration": 0})
                            except Exception:
                                pass
                        # persist llm_usage.json to dest trace dir if different
                        try:
                            dest_dir = None
                            if trace_writer is not None and hasattr(trace_writer, "dir_path"):
                                dest_dir = Path(trace_writer.dir_path)
                            elif trace_writer is not None and hasattr(trace_writer, "path"):
                                dest_dir = Path(trace_writer.path).parent
                            elif isinstance(self.trace, (str, Path)):
                                _pp = Path(self.trace)
                                dest_dir = _pp.parent if _pp.suffix == ".jsonl" else _pp
                            if dest_dir is not None:
                                dest_dir.mkdir(parents=True, exist_ok=True)
                                out_path = dest_dir / "llm_usage.json"
                                try:
                                    same = rp.resolve() == out_path.resolve()
                                except Exception:
                                    same = False
                                if not same:
                                    _replay_json_out = {"text": buffer, "llm_usage": _norm, "chunks": data.get("chunks", []) if isinstance(data, dict) else []}
                                    out_path.write_text(_replay_json.dumps(_replay_json_out, ensure_ascii=False), encoding="utf-8")
                        except Exception:
                            pass
                        # trace_path for result
                        trace_path_str_r: str | None = None
                        if trace_writer is not None:
                            try:
                                _p = getattr(trace_writer, "path", None)
                                if _p is not None:
                                    trace_path_str_r = str(_p)
                            except Exception:
                                pass
                        elif isinstance(self.trace, (str, Path)):
                            trace_path_str_r = str(self.trace)
                        return LoopResult(
                            terminated=True,
                            iterations=1,
                            text=buffer,
                            reason="completed",
                            metrics=None,
                            grounding_verified=True,
                            trace_path=trace_path_str_r,
                            token_count=token_count,
                        )
            except Exception:
                pass

        # lazy init retry_policy if not set (uses pre-imported _RetryPolicy for speed)
        retry_policy = self.retry_policy
        if retry_policy is None:
            try:
                if _RetryPolicy is not None:
                    retry_policy = _RetryPolicy()
                else:
                    from .policies import RetryPolicy as _RP

                    retry_policy = _RP()
            except Exception:
                retry_policy = None

        # Use while not terminated with control points
        while not terminated:
            # 1. max_iterations check
            if iterations >= self.max_iterations:
                reason = "max_iterations"
                terminated = True
                break

            # 2. token_limit check via estimate_tokens(buffer) or context estimate
            if self.token_limit is not None:
                cur_len = estimate_tokens(buffer)
                # also check context char count if available
                ctx_len = 0
                if self.context_manager is not None:
                    try:
                        # ContextManager stores _messages
                        msgs = getattr(self.context_manager, "_messages", None)
                        if isinstance(msgs, list):
                            # estimate from messages
                            ctx_text = "\n".join(str(m.get("content", "")) for m in msgs)
                            ctx_len = estimate_tokens(ctx_text)
                        elif hasattr(self.context_manager, "max_chars"):
                            # fallback use buffer len
                            pass
                    except Exception:
                        ctx_len = 0
                effective = max(cur_len, ctx_len)
                if effective >= int(self.token_limit):
                    # TRUNCATED banner
                    banner = "TRUNCATED: token_limit exceeded"
                    if "TRUNCATED" not in buffer:
                        # truncate buffer to limit and add banner
                        limit = int(self.token_limit)
                        buffer = buffer[:limit] + f"\n[{banner}]"
                    token_count = estimate_tokens(buffer)
                    reason = "token_limit"
                    terminated = True
                    if trace_writer is not None:
                        try:
                            trace_writer.append({"type": "truncated", "reason": "token_limit", "iterations": iterations, "banner": banner})
                        except Exception:
                            pass
                    break

            # 3. user_stop placeholder (check for signal)
            if getattr(self, "_stop_requested", False):
                reason = "user_stop"
                terminated = True
                break
            # also check external signal file or env (placeholder)
            # e.g., check if llm has should_stop attribute
            try:
                if callable(getattr(self.llm, "should_stop", None)) and self.llm.should_stop():  # type: ignore[attr-defined]
                    reason = "user_stop"
                    terminated = True
                    break
            except Exception:
                pass

            iterations += 1

            # trace iteration start
            if trace_writer is not None:
                try:
                    trace_writer.append({"type": "iteration_start", "iteration": iterations, "goal": goal[:500] if isinstance(goal, str) else str(goal)[:500]})
                except Exception:
                    pass

            # 4. llm.stream_chat with try/except -> retry via RetryPolicy
            stream = None
            last_exc: Optional[BaseException] = None
            # attempt loop for acquiring stream
            max_attempts = getattr(retry_policy, "max_attempts", 3) if retry_policy is not None else 3
            acquired = False
            for attempt in range(1, int(max_attempts) + 1):
                try:
                    raw = self._call_llm(goal)
                    stream = self._normalize_stream(raw)
                    acquired = True
                    last_exc = None
                    break
                except BaseException as e:
                    last_exc = e
                    should = False
                    if retry_policy is not None:
                        try:
                            should = bool(retry_policy.should_retry(e, attempt))
                        except Exception:
                            should = attempt < int(max_attempts)
                    else:
                        should = attempt < int(max_attempts)
                    if not should:
                        break
                    # exponential backoff
                    if retry_policy is not None:
                        try:
                            retry_policy.sleep(attempt)
                        except Exception:
                            try:
                                time.sleep(min(0.02 * (2 ** (attempt - 1)), 0.5))
                            except Exception:
                                pass
                    else:
                        try:
                            time.sleep(0.02 * attempt)
                        except Exception:
                            pass
            if not acquired:
                # all retries exhausted
                if last_exc is not None:
                    if trace_writer is not None:
                        try:
                            trace_writer.append({"type": "llm_error", "iteration": iterations, "error": str(last_exc)})
                        except Exception:
                            pass
                    buffer += f"\n[ERROR: {last_exc}]"
                    token_count = estimate_tokens(buffer)
                    reason = "llm_error"
                    # For backward compat simple case, if buffer has text prior, we still mark completed?
                    # But if never succeeded, mark llm_error and terminate
                    terminated = True
                    break
                stream = []

            # 5. accumulate text deltas, update token_count, while iterating chunks
            tool_calls_this_iter: List[Dict[str, Any]] = []
            _chunk_error: Optional[BaseException] = None
            try:
                for chunk in stream:  # type: ignore[union-attr]
                    # chunk may be dict, string, or object
                    if isinstance(chunk, dict):
                        # text extraction
                        txt = None
                        if chunk.get("type") == "text" and "text" in chunk:
                            txt = chunk.get("text", "")
                        elif "text" in chunk and chunk.get("type") is None:
                            # bare text dict
                            txt = chunk.get("text", "")
                        elif chunk.get("delta") is not None:
                            txt = str(chunk.get("delta"))
                        elif chunk.get("content") is not None and isinstance(chunk.get("content"), str):
                            # only if not tool call
                            if not chunk.get("tool_calls"):
                                txt = str(chunk.get("content"))
                        if txt is not None:
                            buffer += str(txt)
                        # tool call detection
                        if chunk.get("tool_calls"):
                            tcs = chunk.get("tool_calls")
                            if isinstance(tcs, list):
                                for tc in tcs:
                                    if isinstance(tc, dict):
                                        tool_calls_this_iter.append(tc)
                                    else:
                                        tool_calls_this_iter.append({"name": str(tc), "arguments": {}})
                            elif isinstance(tcs, dict):
                                tool_calls_this_iter.append(tcs)
                        if chunk.get("type") == "tool_call":
                            tool_calls_this_iter.append(chunk)
                        # also support chunk type tool_calls wrapped
                        if chunk.get("type") == "tool_calls" and chunk.get("calls"):
                            for tc in chunk.get("calls", []):
                                if isinstance(tc, dict):
                                    tool_calls_this_iter.append(tc)
                        # -- llm_usage accumulation (C1-2 compat usage_metadata/usage/prompt_tokens) --
                        try:
                            _um = None
                            if chunk.get("usage_metadata") is not None and isinstance(chunk.get("usage_metadata"), dict):
                                _um = chunk.get("usage_metadata")
                            elif chunk.get("usage") is not None and isinstance(chunk.get("usage"), dict):
                                _um = chunk.get("usage")
                            elif "input_tokens" in chunk or "prompt_tokens" in chunk or "output_tokens" in chunk:
                                _um = chunk
                            if isinstance(_um, dict):
                                _iv = _um.get("input_tokens")
                                if _iv is None:
                                    _iv = _um.get("prompt_tokens", _um.get("promptTokens", 0))
                                _ov = _um.get("output_tokens")
                                if _ov is None:
                                    _ov = _um.get("completion_tokens", _um.get("generated_tokens", 0))
                                try:
                                    _llm_usage_input += int(_iv) if _iv is not None else 0
                                except Exception:
                                    pass
                                try:
                                    _llm_usage_output += int(_ov) if _ov is not None else 0
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    elif isinstance(chunk, str):
                        buffer += chunk
                    else:
                        # try to stringify unknown objects
                        try:
                            buffer += str(chunk)
                        except Exception:
                            pass

                    token_count = estimate_tokens(buffer)

                    # trace each chunk (with size limit to avoid blowup)
                    if trace_writer is not None:
                        try:
                            # chunk preview truncated to avoid trace blowup
                            preview = str(chunk)[:2000]
                            trace_writer.append({"type": "chunk", "iteration": iterations, "chunk": preview})
                        except Exception:
                            pass

                    # mid-stream token_limit check
                    if self.token_limit is not None and estimate_tokens(buffer) >= int(self.token_limit):
                        banner = "TRUNCATED: token_limit exceeded"
                        if "TRUNCATED" not in buffer:
                            buffer = buffer[: int(self.token_limit)] + f"\n[{banner}]"
                        token_count = estimate_tokens(buffer)
                        reason = "token_limit"
                        terminated = True
                        if trace_writer is not None:
                            try:
                                trace_writer.append({"type": "truncated", "reason": "token_limit", "iteration": iterations, "banner": banner})
                            except Exception:
                                pass
                        break

                # if terminated due to token_limit mid-stream, break outer iteration handling
                if terminated and reason == "token_limit":
                    break
            except BaseException as e:
                _chunk_error = e
                should = False
                if retry_policy is not None:
                    try:
                        # use current attempt count; if not enough attempts, retry whole iteration
                        should = bool(retry_policy.should_retry(e, iterations))
                    except Exception:
                        should = False
                if should:
                    try:
                        if retry_policy is not None:
                            retry_policy.sleep(iterations)
                        else:
                            time.sleep(0.02)
                    except Exception:
                        pass
                    # continue to next while iteration (retry)
                    # adjust iterations back? we already incremented, but we want to retry without counting extra? keep as is
                    continue
                else:
                    if trace_writer is not None:
                        try:
                            trace_writer.append({"type": "llm_stream_error", "iteration": iterations, "error": str(e)})
                        except Exception:
                            pass
                    buffer += f"\n[ERROR: {e}]"
                    token_count = estimate_tokens(buffer)
                    reason = "llm_error"
                    terminated = True
                    break

            token_count = estimate_tokens(buffer)

            # 6. tool_calls execution via TOOL_REGISTRY (parallel readonly pool)
            tool_success_this_iter = False
            if tool_calls_this_iter:
                # -- parse all tool calls first (keep 10 control points, audit, grounding intact) --
                parsed: List[Dict[str, Any]] = []
                for tc in tool_calls_this_iter:
                    tool_name = tc.get("name") or tc.get("tool") or tc.get("function") or tc.get("tool_name") or ""
                    if not tool_name and isinstance(tc.get("function"), dict):
                        tool_name = tc["function"].get("name", "")
                    args = tc.get("arguments")
                    if args is None:
                        args = tc.get("args") or tc.get("parameters") or tc.get("input") or {}
                    if isinstance(tc.get("function"), dict) and not args:
                        f = tc["function"]
                        args = f.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            import json as _json

                            args = _json.loads(args) if args.strip() else {}
                        except Exception:
                            args = {}
                    if not isinstance(args, dict):
                        args = {"value": args}
                    if not tool_name:
                        continue
                    spec = None
                    try:
                        from hero_quant.tools.registry import TOOL_REGISTRY

                        spec = TOOL_REGISTRY.get(tool_name)
                    except Exception:
                        spec = None
                    is_safe = False
                    if spec is not None:
                        try:
                            is_safe = bool(spec.is_concurrency_safe(args))
                        except Exception:
                            is_safe = False
                    else:
                        is_safe = False
                    # redact args for trace
                    redacted_args: Any = args
                    try:
                        from hero_quant.security.redaction import redact_payload

                        redacted_args = redact_payload(args, sink="arguments") if isinstance(args, dict) else args
                    except Exception:
                        try:
                            from hero_quant.tools.redaction import _maybe_redact  # type: ignore

                            redacted_args = _maybe_redact(args, sink="arguments")
                        except Exception:
                            redacted_args = args
                    if trace_writer is not None:
                        try:
                            trace_writer.append(
                                {
                                    "type": "tool_call",
                                    "iteration": iterations,
                                    "tool": tool_name,
                                    "arguments": redacted_args,
                                    "concurrency_safe": is_safe,
                                }
                            )
                        except Exception:
                            pass
                    parsed.append(
                        {
                            "tool_name": tool_name,
                            "args": args,
                            "spec": spec,
                            "is_safe": is_safe,
                            "redacted_args": redacted_args,
                        }
                    )

                # split into parallel-safe vs serial (write tools serial)
                concurrent_items: List[Dict[str, Any]] = [p for p in parsed if p["is_safe"] and p["spec"] is not None]
                serial_items: List[Dict[str, Any]] = [p for p in parsed if p not in concurrent_items]

                # helper to execute a single spec and return (result, error)
                def _exec_spec(spec: Any, args: Dict[str, Any]) -> tuple[Any, Optional[BaseException]]:
                    try:
                        res = spec.func(**args) if isinstance(args, dict) else spec.func(args)
                        return res, None
                    except BaseException as e:
                        return f"tool_error: {e}", e

                def _redact_result(result: Any) -> str:
                    try:
                        from hero_quant.tools.redaction import redact_tool_result

                        return redact_tool_result(result, sink="result")
                    except Exception:
                        try:
                            from hero_quant.security.redaction import redact_payload as _rp

                            if isinstance(result, dict):
                                return str(_rp(result, sink="result"))
                            return str(result)
                        except Exception:
                            return str(result)

                def _handle_result(tool_name: str, result: Any, err: Optional[BaseException]):
                    nonlocal tool_success_this_iter, _tool_success_global, buffer
                    if err is None and result is not None and not (isinstance(result, str) and result.startswith("tool_error:")):
                        # success only if spec existed and no exception
                        # for tool_not_found, err is set, so not success
                        tool_success_this_iter = True
                        _tool_success_global = True
                    elif err is None and not isinstance(result, str):
                        tool_success_this_iter = True
                        _tool_success_global = True
                    elif err is None and isinstance(result, str) and result.startswith("tool_not_found:"):
                        pass
                    elif err is None:
                        # result string but not error prefix -> consider success
                        tool_success_this_iter = True
                        _tool_success_global = True
                    redacted_result_str = _redact_result(result)
                    if trace_writer is not None:
                        try:
                            trace_writer.append(
                                {
                                    "type": "tool_result",
                                    "iteration": iterations,
                                    "tool": tool_name,
                                    "content": redacted_result_str,
                                }
                            )
                        except Exception:
                            pass
                    try:
                        snippet = redacted_result_str[:2000] if isinstance(redacted_result_str, str) else str(redacted_result_str)[:2000]
                        buffer += f"\n[tool {tool_name} result] {snippet}"
                    except Exception:
                        pass

                # execute concurrent pool via ThreadPoolExecutor
                if concurrent_items:
                    # use max_workers = number of concurrent items (capped)
                    max_workers = min(len(concurrent_items), 8)
                    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                        future_map: Dict[Any, Dict[str, Any]] = {}
                        for item in concurrent_items:
                            fut = executor.submit(_exec_spec, item["spec"], item["args"])
                            future_map[fut] = item
                        # collect results preserving original order
                        results_map: Dict[str, tuple[Any, Optional[BaseException]]] = {}
                        # Use list of futures in submission order; wait for each
                        for fut, item in future_map.items():
                            try:
                                res, err = fut.result()
                            except BaseException as e:
                                res, err = f"tool_error: {e}", e
                            # key by id(item) to keep ordering later
                            results_map[str(id(item))] = (res, err)
                        # handle in original concurrent_items order to keep buffer deterministic
                        for item in concurrent_items:
                            res, err = results_map[str(id(item))]
                            # _exec_spec returns (result, error); but success detection needs spec existence
                            # if err is not None -> already tool_error string
                            # mark success if err is None
                            if err is None:
                                tool_success_this_iter = True
                                _tool_success_global = True
                            _handle_result(item["tool_name"], res, err)
                            # avoid double marking via _handle_result's own logic for success; we already set

                # execute serial items (write tools / unsafe) sequentially
                for item in serial_items:
                    tool_name = item["tool_name"]
                    args = item["args"]
                    spec = item["spec"]
                    result: Any = None
                    _tool_error: Optional[BaseException] = None
                    if spec is not None:
                        result, _tool_error = _exec_spec(spec, args)
                        if _tool_error is None:
                            tool_success_this_iter = True
                            _tool_success_global = True
                    else:
                        result = f"tool_not_found: {tool_name}"
                        _tool_error = Exception(result)
                    # _handle_result will also handle trace+buffer; but avoid double success marking duplication
                    # we already handled success above, so call helper without extra success side effect? Use inline handling
                    redacted_result_str = _redact_result(result)
                    if trace_writer is not None:
                        try:
                            trace_writer.append(
                                {
                                    "type": "tool_result",
                                    "iteration": iterations,
                                    "tool": tool_name,
                                    "content": redacted_result_str,
                                }
                            )
                        except Exception:
                            pass
                    try:
                        snippet = redacted_result_str[:2000] if isinstance(redacted_result_str, str) else str(redacted_result_str)[:2000]
                        buffer += f"\n[tool {tool_name} result] {snippet}"
                    except Exception:
                        pass

                token_count = estimate_tokens(buffer)

            # 7. grounding_check
            if self.grounding is not None:
                try:
                    # detect price mentions: look for symbol + number
                    symbols: List[str] = []
                    try:
                        ev = getattr(self.grounding, "_evidence", {})
                        if isinstance(ev, dict):
                            symbols = list(ev.keys())
                    except Exception:
                        symbols = []
                    # extract numbers
                    nums = re.findall(r"\d+\.?\d*", buffer)
                    verified = False
                    # if no symbols but grounding exists, try generic
                    if symbols:
                        for sym in symbols:
                            if sym in buffer:
                                for n in nums:
                                    try:
                                        price = float(n)
                                        self.grounding.assert_price(sym, price)
                                        verified = True
                                        break
                                    except Exception:
                                        continue
                                if verified:
                                    break
                        # fallback: if numbers exist but symbol not explicitly mentioned, try first symbol
                        if not verified and nums:
                            for n in nums:
                                try:
                                    price = float(n)
                                    self.grounding.assert_price(symbols[0], price)
                                    verified = True
                                    break
                                except Exception:
                                    continue
                    else:
                        # no evidence yet -> consider not verified
                        verified = False
                    grounding_verified = bool(verified)
                    # if price mentions exist but verification failed, keep false
                    # if no price mentions, consider passed (no hallucination)
                    if not nums:
                        grounding_verified = True
                    if trace_writer is not None:
                        try:
                            trace_writer.append({"type": "grounding", "iteration": iterations, "verified": grounding_verified})
                        except Exception:
                            pass
                except BaseException as e:
                    grounding_verified = False
                    if trace_writer is not None:
                        try:
                            trace_writer.append({"type": "grounding_error", "iteration": iterations, "error": str(e)})
                        except Exception:
                            pass
            else:
                # no grounding required -> pass
                grounding_verified = True

            # 8. context_compact
            if self.context_manager is not None and self.token_limit is not None:
                try:
                    # ensure context has some messages for char count
                    # add current buffer as assistant message if not already (best-effort)
                    try:
                        if hasattr(self.context_manager, "add"):
                            # avoid duplicate add if buffer empty
                            pass
                    except Exception:
                        pass
                    if estimate_tokens(buffer) > int(self.token_limit) * 0.8:
                        # call compact
                        cr = self.context_manager.compact()
                        if getattr(cr, "truncated", False):
                            banner = getattr(cr, "banner", "TRUNCATED: context folded")
                            if "TRUNCATED" not in buffer:
                                buffer = f"[{banner}]\n" + buffer
                            token_count = estimate_tokens(buffer)
                            if trace_writer is not None:
                                try:
                                    trace_writer.append({"type": "context_compact", "iteration": iterations, "banner": banner, "truncated": True})
                                except Exception:
                                    pass
                        else:
                            if trace_writer is not None:
                                try:
                                    trace_writer.append({"type": "context_compact", "iteration": iterations, "truncated": False})
                                except Exception:
                                    pass
                except BaseException as e:
                    if trace_writer is not None:
                        try:
                            trace_writer.append({"type": "context_error", "iteration": iterations, "error": str(e)})
                        except Exception:
                            pass

            # 9. budget_breaker check
            if self.budget_breaker is not None:
                try:
                    estimated = token_count / 10000.0 + iterations * 0.05
                    # also consider buffer length
                    if hasattr(self.budget_breaker, "should_fallback"):
                        if self.budget_breaker.should_fallback(cost=estimated):
                            reason = "budget_fallback"
                            terminated = True
                            if trace_writer is not None:
                                try:
                                    trace_writer.append({"type": "budget", "iteration": iterations, "cost": estimated, "fallback": True, "reason": "budget_fallback"})
                                except Exception:
                                    pass
                            break
                except Exception:
                    pass

            # post-iteration token_limit guard (if buffer grew after tools/compact)
            if self.token_limit is not None and estimate_tokens(buffer) >= int(self.token_limit):
                banner = "TRUNCATED: token_limit exceeded"
                if "TRUNCATED" not in buffer:
                    buffer = buffer[: int(self.token_limit)] + f"\n[{banner}]"
                token_count = estimate_tokens(buffer)
                reason = "token_limit"
                terminated = True
                if trace_writer is not None:
                    try:
                        trace_writer.append({"type": "truncated", "reason": "token_limit", "iteration": iterations, "banner": banner})
                    except Exception:
                        pass
                break

            # 10. tool_success+grounding_pass -> terminated
            if tool_calls_this_iter:
                if tool_success_this_iter and grounding_verified:
                    terminated = True
                    reason = "completed"
                    break
                # if tools succeeded but grounding failed and grounding required, continue unless max reached
                if tool_success_this_iter and not grounding_verified and self.grounding is not None:
                    # continue loop for correction
                    # if we have exhausted iterations, will hit max next loop
                    # mark reason but not terminated
                    # avoid infinite: if iterations already high, break with grounding_failed
                    if iterations >= self.max_iterations:
                        reason = "grounding_failed"
                        terminated = True
                        break
                    # continue to next iteration to let LLM correct
                    continue
                # tool_success without grounding (no grounding) -> already handled
                if tool_success_this_iter:
                    terminated = True
                    reason = "completed"
                    break
            else:
                # No tool calls this iteration
                if buffer.strip():
                    # simple path: any text means completed (backward compat)
                    terminated = True
                    reason = "completed"
                    break
                # empty buffer -> continue or break? avoid infinite empty loop
                # if llm keeps returning empty, we'll hit max_iterations
                continue

        # end while

        # handle max_iterations edge (if not terminated but loop exited)
        if not terminated and iterations >= self.max_iterations:
            reason = "max_iterations"
            terminated = True

        # ensure token_count consistent
        token_count = estimate_tokens(buffer)

        # -- C1-2 VCR: write llm_usage trace + llm_usage.json (if accumulated) --
        try:
            if (_llm_usage_input or _llm_usage_output):
                _llm_usage_dict = {"input_tokens": int(_llm_usage_input), "output_tokens": int(_llm_usage_output)}
                if trace_writer is not None:
                    try:
                        trace_writer.append({"type": "llm_usage", "llm_usage": _llm_usage_dict})
                    except Exception:
                        pass
                # write llm_usage.json alongside trace dir
                try:
                    import json as _vcr_json

                    _dest_dir = None
                    if trace_writer is not None and hasattr(trace_writer, "dir_path"):
                        _dest_dir = Path(trace_writer.dir_path)
                    elif trace_writer is not None and hasattr(trace_writer, "path"):
                        _dest_dir = Path(trace_writer.path).parent
                    elif isinstance(self.trace, (str, Path)):
                        _pp2 = Path(self.trace)
                        _dest_dir = _pp2.parent if _pp2.suffix == ".jsonl" else _pp2
                    if _dest_dir is not None:
                        _dest_dir.mkdir(parents=True, exist_ok=True)
                        _out2 = _dest_dir / "llm_usage.json"
                        _payload2 = {"llm_usage": _llm_usage_dict, "text": buffer}
                        _out2.write_text(_vcr_json.dumps(_payload2, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass
        except Exception:
            pass

        # grounding_verified final: if grounding is None, True else last value
        if self.grounding is None:
            grounding_verified = True

        # trace_path
        trace_path_str: Optional[str] = None
        if trace_writer is not None:
            try:
                p = getattr(trace_writer, "path", None)
                if p is not None:
                    trace_path_str = str(p)
                else:
                    dp = getattr(trace_writer, "dir_path", None)
                    if dp is not None:
                        trace_path_str = str(dp)
            except Exception:
                trace_path_str = None
        elif isinstance(self.trace, (str, Path)):
            trace_path_str = str(self.trace)

        # metrics placeholder (could extract sharpe etc)
        # try to parse metrics from buffer (e.g., sharpe)
        if metrics is None:
            # minimal: if buffer contains sharpe, extract
            try:
                m = re.search(r"sharpe\s*[:=]?\s*([-\d\.]+)", buffer, re.IGNORECASE)
                if m:
                    metrics = {"sharpe": float(m.group(1))}
            except Exception:
                metrics = None

        # ensure terminated True for simple success path (tests expect terminated True)
        # For max_iterations/token_limit/budget_fallback we also set terminated True (loop ended)
        # but reason distinguishes.

        return LoopResult(
            terminated=bool(terminated) if reason != "max_iterations" else True,
            iterations=iterations,
            text=buffer,
            reason=reason,
            metrics=metrics,
            grounding_verified=bool(grounding_verified),
            trace_path=trace_path_str,
            token_count=token_count,
        )

    # -- graph delegation --
    def _run_graph(self, goal: str) -> LoopResult:
        trace_writer = self._ensure_trace_writer()
        g = self.graph
        if g is None:
            try:
                from .graph import build_research_graph

                g = build_research_graph()
            except Exception as e:
                return LoopResult(
                    terminated=True,
                    iterations=0,
                    text=f"graph_build_error: {e}",
                    reason="graph_error",
                    token_count=0,
                )
        # invoke graph
        result: Any = None
        try:
            # try standard invoke with state dict
            state = {"messages": [{"role": "user", "content": goal}]}
            # langgraph compiled graph expects invoke(state)
            try:
                result = g.invoke(state)  # type: ignore[attr-defined]
            except TypeError:
                # try with invoke({"messages":...}, config)
                result = g.invoke(state, config={})  # type: ignore
        except BaseException as e:
            if trace_writer is not None:
                try:
                    trace_writer.append({"type": "graph_error", "error": str(e)})
                except Exception:
                    pass
            return LoopResult(terminated=True, iterations=1, text=f"graph_error: {e}", reason="graph_error", token_count=len(str(e)))

        # translate result to LoopResult
        text = ""
        try:
            if isinstance(result, dict):
                msgs = result.get("messages") or result.get("msgs") or []
                if isinstance(msgs, list) and msgs:
                    # take last assistant message content
                    last = msgs[-1]
                    if isinstance(last, dict):
                        text = str(last.get("content", "") or last.get("text", "") or "")
                    else:
                        text = str(last)
                else:
                    # fallback to string of result
                    text = str(result)
                # try grounding verification from result
            else:
                text = str(result)
        except Exception:
            text = str(result) if result is not None else ""

        if trace_writer is not None:
            try:
                trace_writer.append({"type": "graph_result", "goal": goal[:500], "text": text[:2000]})
            except Exception:
                pass

        trace_path_str = None
        if trace_writer is not None:
            try:
                p = getattr(trace_writer, "path", None)
                if p is not None:
                    trace_path_str = str(p)
            except Exception:
                pass

        token_count = estimate_tokens(text)
        # grounding_verified if graph grounding passed
        grounding_verified = True
        if self.grounding is not None and text:
            try:
                # simple check if text contains price, verify
                nums = re.findall(r"\d+\.?\d*", text)
                if nums:
                    # try first symbol from grounding evidence
                    ev = getattr(self.grounding, "_evidence", {})
                    if isinstance(ev, dict) and ev:
                        sym = list(ev.keys())[0]
                        for n in nums:
                            try:
                                self.grounding.assert_price(sym, float(n))
                                grounding_verified = True
                                break
                            except Exception:
                                grounding_verified = False
                                continue
            except Exception:
                grounding_verified = False

        # extract metrics if present
        metrics = None
        try:
            m = re.search(r"sharpe\s*[:=]?\s*([-\d\.]+)", text, re.IGNORECASE)
            if m:
                metrics = {"sharpe": float(m.group(1))}
        except Exception:
            pass

        return LoopResult(
            terminated=True,
            iterations=1,
            text=text if text else str(result) if result else goal,
            reason="completed",
            metrics=metrics,
            grounding_verified=grounding_verified,
            trace_path=trace_path_str,
            token_count=token_count,
        )

