"""AgentLoop state machine - production hardened (Wave A3).

Port of vibe-trading loop.py 8 control points + context/grounding/trace/policy.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


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
      2. token_limit check via len(buffer) or context char count -> reason="token_limit" + TRUNCATED banner
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
        tool_success_global = False

        trace_writer = self._ensure_trace_writer()

        # lazy init retry_policy if not set
        retry_policy = self.retry_policy
        if retry_policy is None:
            try:
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

            # 2. token_limit check via len(buffer) or context char count
            if self.token_limit is not None:
                cur_len = len(buffer)
                # also check context char count if available
                ctx_len = 0
                if self.context_manager is not None:
                    try:
                        # ContextManager stores _messages
                        msgs = getattr(self.context_manager, "_messages", None)
                        if isinstance(msgs, list):
                            # estimate from messages
                            ctx_text = "\n".join(str(m.get("content", "")) for m in msgs)
                            ctx_len = len(ctx_text)
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
                    token_count = len(buffer)
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
                    token_count = len(buffer)
                    reason = "llm_error"
                    # For backward compat simple case, if buffer has text prior, we still mark completed?
                    # But if never succeeded, mark llm_error and terminate
                    terminated = True
                    break
                stream = []

            # 5. accumulate text deltas, update token_count, while iterating chunks
            tool_calls_this_iter: List[Dict[str, Any]] = []
            chunk_error: Optional[BaseException] = None
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
                    elif isinstance(chunk, str):
                        buffer += chunk
                    else:
                        # try to stringify unknown objects
                        try:
                            buffer += str(chunk)
                        except Exception:
                            pass

                    token_count = len(buffer)

                    # trace each chunk (with size limit to avoid blowup)
                    if trace_writer is not None:
                        try:
                            # chunk preview truncated to avoid trace blowup
                            preview = str(chunk)[:2000]
                            trace_writer.append({"type": "chunk", "iteration": iterations, "chunk": preview})
                        except Exception:
                            pass

                    # mid-stream token_limit check
                    if self.token_limit is not None and len(buffer) >= int(self.token_limit):
                        banner = "TRUNCATED: token_limit exceeded"
                        if "TRUNCATED" not in buffer:
                            buffer = buffer[: int(self.token_limit)] + f"\n[{banner}]"
                        token_count = len(buffer)
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
                chunk_error = e
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
                    token_count = len(buffer)
                    reason = "llm_error"
                    terminated = True
                    break

            token_count = len(buffer)

            # 6. tool_calls execution via TOOL_REGISTRY
            tool_success_this_iter = False
            if tool_calls_this_iter:
                for tc in tool_calls_this_iter:
                    tool_name = tc.get("name") or tc.get("tool") or tc.get("function") or tc.get("tool_name") or ""
                    # some tool call shapes: {"type":"tool_call","name":"x","arguments":{}}
                    # other: {"id":..., "function":{"name":"x","arguments":"{}"}}
                    if not tool_name and isinstance(tc.get("function"), dict):
                        tool_name = tc["function"].get("name", "")
                    args = tc.get("arguments")
                    if args is None:
                        args = tc.get("args") or tc.get("parameters") or tc.get("input") or {}
                    if isinstance(tc.get("function"), dict) and not args:
                        # OpenAI style
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

                    # lookup TOOL_REGISTRY
                    spec = None
                    try:
                        from hero_quant.tools.registry import TOOL_REGISTRY

                        spec = TOOL_REGISTRY.get(tool_name)
                    except Exception:
                        spec = None

                    is_safe = True
                    if spec is not None:
                        try:
                            is_safe = bool(spec.is_concurrency_safe(args))
                        except Exception:
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

                    # execute
                    result: Any = None
                    tool_error: Optional[BaseException] = None
                    if spec is not None:
                        try:
                            result = spec.func(**args) if isinstance(args, dict) else spec.func(args)
                            tool_success_this_iter = True
                            tool_success_global = True
                        except BaseException as e:
                            tool_error = e
                            result = f"tool_error: {e}"
                    else:
                        result = f"tool_not_found: {tool_name}"
                        tool_error = Exception(result)

                    # redact result
                    redacted_result_str: str
                    try:
                        from hero_quant.tools.redaction import redact_tool_result

                        # redact_tool_result returns str
                        redacted_result_str = redact_tool_result(result, sink="result")
                    except Exception:
                        try:
                            from hero_quant.security.redaction import redact_payload as _rp

                            if isinstance(result, dict):
                                redacted_result_str = str(_rp(result, sink="result"))
                            else:
                                redacted_result_str = str(result)
                        except Exception:
                            redacted_result_str = str(result)

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

                    # accumulate to buffer (so grounding/context can see)
                    try:
                        snippet = redacted_result_str[:2000] if isinstance(redacted_result_str, str) else str(redacted_result_str)[:2000]
                        buffer += f"\n[tool {tool_name} result] {snippet}"
                    except Exception:
                        pass

                token_count = len(buffer)

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
                    if len(buffer) > int(self.token_limit) * 0.8:
                        # call compact
                        cr = self.context_manager.compact()
                        if getattr(cr, "truncated", False):
                            banner = getattr(cr, "banner", "TRUNCATED: context folded")
                            if "TRUNCATED" not in buffer:
                                buffer = f"[{banner}]\n" + buffer
                            token_count = len(buffer)
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
            if self.token_limit is not None and len(buffer) >= int(self.token_limit):
                banner = "TRUNCATED: token_limit exceeded"
                if "TRUNCATED" not in buffer:
                    buffer = buffer[: int(self.token_limit)] + f"\n[{banner}]"
                token_count = len(buffer)
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
        token_count = len(buffer)

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

        token_count = len(text)
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

