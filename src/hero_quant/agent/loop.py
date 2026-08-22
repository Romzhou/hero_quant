"""AgentLoop 状态机：驱动 LLM↔工具往返循环直至满足终止条件。

在架构中的位置：agent 层核心循环，承接上游 goal 与上下文，串联 LLM 流式输出、
TOOL_REGISTRY 工具调度、GroundingLedger 价格校验、ContextManager 上下文压缩、
TraceWriter 轨迹落盘、RetryPolicy 重试与 BudgetBreaker 预算熔断。

关键设计决策：控制点设计参考 vibe-trading 项目；token 粗估按 ~4 字符/token（JSON 序列化长度 //4）；
只读工具并发执行且受 spec.timeoutMs 约束，写工具串行；重试/退避与错误收敛统一收口为 tool_error。
"""

from __future__ import annotations

import concurrent.futures
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

# 模块加载时预导入 RetryPolicy，避免 run() 热路径临时导入导致并行场景壁时间超过 0.35s
try:
    from .policies import RetryPolicy as _RetryPolicy  # type: ignore
except Exception:  # pragma: no cover
    _RetryPolicy = None  # type: ignore


def estimate_tokens(text: Any) -> int:
    """估算文本 token 数，约 4 字符/token（JSON 序列化长度 //4 粗估）。

    支持 str、消息列表及其他类型，异常时回退为 str(text) 长度 //4。
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
    """AgentLoop 单次执行的返回结果，承载终止状态、输出文本与校验/用量信息。"""

    terminated: bool
    iterations: int
    text: str = ""
    reason: str = ""
    metrics: Optional[Dict[str, Any]] = None
    grounding_verified: bool = False
    trace_path: Optional[str] = None
    token_count: int = 0


class AgentLoop:
    """Agent 状态机，驱动 LLM 流式输出与工具执行的往返循环直至终止。

    主循环控制点（按执行顺序，触发时对应 reason）：
    1) 壁时间预算检查 -> wall_time_budget_exceeded
    2) 最大轮次检查 -> max_iterations
    3) token 上限检查（buffer/context 估算）-> token_limit（附 TRUNCATED 标识）
    4) 用户停止信号（_stop_requested / llm.should_stop）-> user_stop
    5) LLM 调用与重试（RetryPolicy.should_retry + 指数退避）失败 -> llm_error
    6) 流式增量累积与 token 计数（含中途 token_limit 熔断）
    7) 工具调用执行（并发只读/串行写入、timeout 熔断、脱敏落盘，异常统一为 tool_error）
    8) 接地性校验（GroundingLedger.assert_price）失败进入纠正轮，耗尽轮次 -> grounding_failed
    9) 上下文压缩（ContextManager.compact，阈值 > token_limit*0.8）
    10) 预算熔断（BudgetBreaker.should_fallback）-> budget_fallback
    11) 工具成功且校验通过或纯文本有输出 -> completed；graph 委托失败 -> graph_error
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
        # 兼容历史别名：context / contextManager
        if context_manager is None and "context" in kwargs:
            context_manager = kwargs.pop("context")
        if context_manager is None and "contextManager" in kwargs:
            context_manager = kwargs.pop("contextManager")
        self.context_manager = context_manager
        # 对外同时暴露 .context 以兼容旧调用方
        self.context = context_manager
        self.grounding = grounding
        self.use_graph = bool(use_graph)
        self.graph = graph
        self.budget_breaker = budget_breaker
        self.retry_policy = retry_policy
        # 兼容驼峰别名 budgetBreaker / retryPolicy
        if self.budget_breaker is None and "budgetBreaker" in kwargs:
            self.budget_breaker = kwargs.pop("budgetBreaker")
        if self.retry_policy is None and "retryPolicy" in kwargs:
            self.retry_policy = kwargs.pop("retryPolicy")
        # 回放兼容：支持 replay_path / replay_from / replay_file 及 replay 标志的多种写法
        _replay_path = kwargs.pop("replay_path", None)
        if _replay_path is None:
            _replay_path = kwargs.pop("replay_from", None)
        if _replay_path is None:
            _replay_path = kwargs.pop("replay_file", None)
        _replay_flag = kwargs.pop("replay", None)
        if _replay_path is None and isinstance(_replay_flag, (str, Path)):
            _replay_path = _replay_flag
        # 若仅传入 replay=True 而无路径，则保持空路径
        self._replay_path = Path(_replay_path) if _replay_path is not None else None
        self.replay_path = self._replay_path
        # 用户主动停止信号
        self._stop_requested = bool(kwargs.pop("stop_requested", False))
        # 壁时间预算：优先级 kwargs > 环境变量 > Settings
        _wt = kwargs.pop("wall_time_budget", None)
        if _wt is None:
            _wt = kwargs.pop("wall_time_budget_seconds", None)
        if _wt is None:
            _wt = kwargs.pop("wallTimeBudget", None)
        if _wt is None:
            try:
                import os as _os

                raw = _os.environ.get("HERO_WALL_TIME_BUDGET", _os.environ.get("HERO_WALL_TIME_BUDGET_SECONDS", "")).strip()
                if raw:
                    _wt = float(raw)
            except Exception:
                _wt = None
        if _wt is None:
            try:
                from hero_quant.config.settings import Settings as _S

                _s = _S()
                _wt = getattr(_s, "wall_time_budget_seconds", None) or getattr(_s, "wall_time_budget", None)
            except Exception:
                _wt = None
        # 归一化：0 或负数视为不限时
        try:
            if _wt is not None:
                _wt_f = float(_wt)
                self.wall_time_budget = _wt_f if _wt_f > 0 else None
            else:
                self.wall_time_budget = None
        except Exception:
            self.wall_time_budget = None
        self.wall_time_budget_seconds = self.wall_time_budget
        # 延迟初始化轨迹写入器
        self._trace_writer = None
        self._init_trace_writer()

    def _init_trace_writer(self):
        """初始化轨迹写入器，兼容 TraceWriter 实例、类鸭类型对象及路径字符串。"""

        if self.trace is None:
            self._trace_writer = None
            return
        # 已是具备 append/path 的 TraceWriter
        if hasattr(self.trace, "append") and hasattr(self.trace, "path"):
            self._trace_writer = self.trace
            return
        if hasattr(self.trace, "append") and callable(getattr(self.trace, "append")):
            # 仅有 append 的鸭类型写入器
            self._trace_writer = self.trace
            return
        # 路径字符串/Path 则构造 TraceWriter
        try:
            from .trace import TraceWriter

            p = Path(self.trace) if isinstance(self.trace, (str, Path)) else Path(str(self.trace))
            self._trace_writer = TraceWriter(p)
        except Exception:
            self._trace_writer = None

    def _ensure_trace_writer(self):
        """返回已初始化的轨迹写入器。"""

        return self._trace_writer

    def request_stop(self):
        """请求终止循环，下次迭代进入 user_stop 分支。"""

        self._stop_requested = True

    def _call_llm(self, goal: str):
        """调用 LLM 获取流式输出，兼容多种方法名与参数签名。"""

        # 按优先级尝试不同方法名，兼容不同 LLM 适配器
        for method_name in ("stream_chat", "invoke", "chat", "__call__"):
            fn = getattr(self.llm, method_name, None)
            if fn is None or not callable(fn):
                continue
            # 兼容位置参数、消息字典与关键字参数三种调用形式
            try:
                res = fn(goal)
            except TypeError:
                try:
                    res = fn({"messages": [{"role": "user", "content": goal}]})
                except Exception:
                    try:
                        res = fn(prompt=goal)
                    except Exception as e:
                        raise e
            return res
        raise AttributeError("llm has no stream_chat/invoke/chat method")

    def _normalize_stream(self, stream) -> List[Dict[str, Any]]:
        """将 LLM 返回归一化为可迭代的 chunk 字典序列。"""

        if stream is None:
            return []
        if isinstance(stream, dict):
            return [stream]
        if isinstance(stream, str):
            return [{"type": "text", "text": stream}]
        return stream  # type: ignore[return-value]

    def run(self, goal: str) -> LoopResult:
        """执行主循环，驱动 LLM↔工具往返直至终止条件满足。"""

        # 使用 monotonic 计时避免系统时间跳变影响壁时间预算
        _wall_start = time.monotonic()
        _wall_budget = getattr(self, "wall_time_budget", None)
        def _wall_exceeded() -> bool:
            if _wall_budget is None:
                return False
            try:
                return (time.monotonic() - _wall_start) > float(_wall_budget)
            except Exception:
                return False

        def _wall_remaining() -> float | None:
            if _wall_budget is None:
                return None
            try:
                return float(_wall_budget) - (time.monotonic() - _wall_start)
            except Exception:
                return None

        # 图委托路径，同样受壁时间预算约束
        if self.use_graph:
            # 进入图前先检查壁时间
            if _wall_exceeded():
                _elapsed = time.monotonic() - _wall_start
                try:
                    from hero_quant.metrics import inc_wall_time_exceeded, observe_wall_time

                    inc_wall_time_exceeded("agent_loop")
                    observe_wall_time("agent_loop", float(_elapsed), status="exceeded")
                except Exception:
                    pass
                return LoopResult(terminated=True, iterations=0, text="", reason="wall_time_budget_exceeded", token_count=0)
            res = self._run_graph(goal)
            # 图执行后上报壁时间
            try:
                _elapsed = time.monotonic() - _wall_start
                from hero_quant.metrics import observe_wall_time

                observe_wall_time("agent_loop", float(_elapsed), status=res.reason if res.reason in ("wall_time_budget_exceeded",) else "success")
                if res.reason == "wall_time_budget_exceeded":
                    from hero_quant.metrics import inc_wall_time_exceeded

                    inc_wall_time_exceeded("agent_loop")
            except Exception:
                pass
            # 若图执行期间已超限则覆盖 reason
            if _wall_exceeded() and res.reason == "completed":
                _elapsed = time.monotonic() - _wall_start
                try:
                    from hero_quant.metrics import inc_wall_time_exceeded

                    inc_wall_time_exceeded("agent_loop")
                except Exception:
                    pass
                res.reason = "wall_time_budget_exceeded"
                res.terminated = True
            return res

        buffer = ""
        iterations = 0
        token_count = 0
        grounding_verified = False
        metrics: Optional[Dict[str, Any]] = None
        reason = "completed"
        terminated = False
        _tool_success_global = False
        # 回放/用量累计：兼容不同字段名的输入/输出 token 计数
        _llm_usage_input = 0
        _llm_usage_output = 0

        trace_writer = self._ensure_trace_writer()

        # 回放短路：若提供回放路径则直接复用历史结果，避免真实 LLM 调用
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
                        # 兼容不同命名：promptTokens/completion_tokens/generated_tokens 等
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
                        # 提取回放文本
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
                        # 若目标轨迹目录不同则同步落盘 llm_usage.json
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

        # 延迟初始化重试策略，利用模块预导入避免热路径开销
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

        # 主循环：按控制点顺序检查终止条件
        while not terminated:
            # 0) 壁时间预算检查
            if _wall_exceeded():
                reason = "wall_time_budget_exceeded"
                terminated = True
                # 上报壁时间超限指标并落盘
                try:
                    _elapsed = time.monotonic() - _wall_start
                    from hero_quant.metrics import inc_wall_time_exceeded, observe_wall_time

                    inc_wall_time_exceeded("agent_loop")
                    observe_wall_time("agent_loop", float(_elapsed), status="exceeded")
                    if trace_writer is not None:
                        trace_writer.append({"type": "wall_time", "reason": "wall_time_budget_exceeded", "elapsed": float(_elapsed), "budget": float(_wall_budget) if _wall_budget else None})
                except Exception:
                    pass
                break
            # 1) 最大轮次检查
            if iterations >= self.max_iterations:
                reason = "max_iterations"
                terminated = True
                break

            # 2) token 上限检查，取 buffer 与 context 的较大值
            if self.token_limit is not None:
                cur_len = estimate_tokens(buffer)
                # 同时估算上下文长度，避免上下文膨胀绕过限制
                ctx_len = 0
                if self.context_manager is not None:
                    try:
                        msgs = getattr(self.context_manager, "_messages", None)
                        if isinstance(msgs, list):
                            ctx_text = "\n".join(str(m.get("content", "")) for m in msgs)
                            ctx_len = estimate_tokens(ctx_text)
                        elif hasattr(self.context_manager, "max_chars"):
                            pass
                    except Exception:
                        ctx_len = 0
                effective = max(cur_len, ctx_len)
                if effective >= int(self.token_limit):
                    banner = "TRUNCATED: token_limit exceeded"
                    if "TRUNCATED" not in buffer:
                        # 截断输出并附加截断标识，避免末尾无限增长
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

            # 3) 用户停止信号检查
            if getattr(self, "_stop_requested", False):
                reason = "user_stop"
                terminated = True
                break
            # 兼容 LLM 适配器对外暴露的 should_stop 回调
            try:
                if callable(getattr(self.llm, "should_stop", None)) and self.llm.should_stop():  # type: ignore[attr-defined]
                    reason = "user_stop"
                    terminated = True
                    break
            except Exception:
                pass

            iterations += 1

            # 记录迭代起点，便于轨迹回放定位
            if trace_writer is not None:
                try:
                    trace_writer.append({"type": "iteration_start", "iteration": iterations, "goal": goal[:500] if isinstance(goal, str) else str(goal)[:500]})
                except Exception:
                    pass

            # 4) LLM 流式调用，失败按 RetryPolicy 重试
            stream = None
            last_exc: Optional[BaseException] = None
            # 尝试获取流：按 max_attempts 重试
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
                    # 指数退避，避免对 LLM 瞬时过载
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
                # 重试耗尽仍未获取到流
                if last_exc is not None:
                    if trace_writer is not None:
                        try:
                            trace_writer.append({"type": "llm_error", "iteration": iterations, "error": str(last_exc)})
                        except Exception:
                            pass
                    buffer += f"\n[ERROR: {last_exc}]"
                    token_count = estimate_tokens(buffer)
                    reason = "llm_error"
                    terminated = True
                    break
                stream = []

            # 5) 累积流式增量、更新 token 计数并收集工具调用
            tool_calls_this_iter: List[Dict[str, Any]] = []
            _chunk_error: Optional[BaseException] = None
            try:
                for chunk in stream:  # type: ignore[union-attr]
                    # chunk 可能是 dict/str/对象，需分别处理
                    if isinstance(chunk, dict):
                        # 提取文本增量，兼容 type/text/delta/content 多种形态
                        txt = None
                        if chunk.get("type") == "text" and "text" in chunk:
                            txt = chunk.get("text", "")
                        elif "text" in chunk and chunk.get("type") is None:
                            txt = chunk.get("text", "")
                        elif chunk.get("delta") is not None:
                            txt = str(chunk.get("delta"))
                        elif chunk.get("content") is not None and isinstance(chunk.get("content"), str):
                            if not chunk.get("tool_calls"):
                                txt = str(chunk.get("content"))
                        if txt is not None:
                            buffer += str(txt)
                        # 识别工具调用，兼容多种包裹形态
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
                        if chunk.get("type") == "tool_calls" and chunk.get("calls"):
                            for tc in chunk.get("calls", []):
                                if isinstance(tc, dict):
                                    tool_calls_this_iter.append(tc)
                        # 累计用量，兼容 usage_metadata/usage/prompt_tokens 等字段
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
                        try:
                            buffer += str(chunk)
                        except Exception:
                            pass

                    token_count = estimate_tokens(buffer)

                    # 落盘 chunk 预览并截断，避免轨迹膨胀
                    if trace_writer is not None:
                        try:
                            preview = str(chunk)[:2000]
                            trace_writer.append({"type": "chunk", "iteration": iterations, "chunk": preview})
                        except Exception:
                            pass

                    # 流中 token 熔断检查
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
                        # 流异常按当前轮次判断是否可重试
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
                    # 可重试则进入下一轮循环
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

            # 6) 工具调用：只读并发、写入串行
            tool_success_this_iter = False
            if tool_calls_this_iter:
                # 先统一解析全部工具调用，确定并发安全性并脱敏落盘
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
                    # 脱敏后落盘，避免敏感信息泄露
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

                # 拆分为可并发与需串行两组
                concurrent_items: List[Dict[str, Any]] = [p for p in parsed if p["is_safe"] and p["spec"] is not None]
                # 使用 id 去重避免相同 payload 的字典相等误判；并发执行受 spec.timeoutMs 约束
                c_ids = {id(c) for c in concurrent_items}
                serial_items: List[Dict[str, Any]] = [p for p in parsed if id(p) not in c_ids]

                # 单个工具执行辅助：返回 (结果, 异常)
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
                    # err 为 None 即成功，统一收敛错误分支
                    if err is None:
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

                # 并发执行只读工具
                if concurrent_items:
                    # 线程数不超过并发项数量且上限 8，避免过度并发
                    max_workers = min(len(concurrent_items), 8)
                    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                        future_map: Dict[Any, Dict[str, Any]] = {}
                        for item in concurrent_items:
                            fut = executor.submit(_exec_spec, item["spec"], item["args"])
                            future_map[fut] = item
                        # 按提交顺序收集结果，并强制执行 spec.timeoutMs 超时
                        results_map: Dict[str, tuple[Any, Optional[BaseException]]] = {}
                        for fut, item in future_map.items():
                            t_ms = getattr(item["spec"], "timeoutMs", None)
                            try:
                                if t_ms is not None:
                                    res, err = fut.result(timeout=t_ms / 1000)
                                else:
                                    res, err = fut.result()
                            except concurrent.futures.TimeoutError as e:
                                # 超时转为 tool_error: timeout
                                res, err = f"tool_error: timeout after {t_ms}ms", e
                                try:
                                    fut.cancel()
                                except Exception:
                                    pass
                            except BaseException as e:
                                res, err = f"tool_error: {e}", e
                            results_map[str(id(item))] = (res, err)
                        # 按原始顺序写入 buffer，保证确定性
                        for item in concurrent_items:
                            res, err = results_map[str(id(item))]
                            _handle_result(item["tool_name"], res, err)

                # 串行执行写工具/非安全工具
                for item in serial_items:
                    tool_name = item["tool_name"]
                    args = item["args"]
                    spec = item["spec"]
                    result: Any = None
                    _tool_error: Optional[BaseException] = None
                    if spec is not None:
                        result, _tool_error = _exec_spec(spec, args)
                    else:
                        result = f"tool_not_found: {tool_name}"
                        _tool_error = Exception(result)
                    _handle_result(tool_name, result, _tool_error)

                token_count = estimate_tokens(buffer)

            # 7) 接地性校验
            if self.grounding is not None:
                try:
                    # 检测是否提及价格：需同时出现 symbol 与数字
                    symbols: List[str] = []
                    try:
                        ev = getattr(self.grounding, "_evidence", {})
                        if isinstance(ev, dict):
                            symbols = list(ev.keys())
                    except Exception:
                        symbols = []
                    nums = re.findall(r"\d+\.?\d*", buffer)
                    verified = False
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
                        # 回退：数字存在但未显式提及 symbol 时尝试首个 symbol
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
                    # 未提及价格则视为无幻觉，直接通过
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
                # 未配置校验则视为通过
                grounding_verified = True

            # 8) 上下文压缩（阈值 0.8*token_limit）
            if self.context_manager is not None and self.token_limit is not None:
                try:
                    # 兼容不同 ContextManager 实现，尽力保证消息存在
                    try:
                        if hasattr(self.context_manager, "add"):
                            pass
                    except Exception:
                        pass
                    if estimate_tokens(buffer) > int(self.token_limit) * 0.8:
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

            # 9) 预算熔断检查
            if self.budget_breaker is not None:
                try:
                    # 简易成本估算：token 与轮次共同决定
                    estimated = token_count / 10000.0 + iterations * 0.05
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

            # 迭代末尾再检 token 上限（工具输出/压缩后可能膨胀）
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

            # 10) 终止判定：工具成功且校验通过或纯文本完成
            if tool_calls_this_iter:
                if tool_success_this_iter and grounding_verified:
                    terminated = True
                    reason = "completed"
                    break
                if tool_success_this_iter and not grounding_verified and self.grounding is not None:
                    # 校验未通过则进入纠正轮，若已达最大轮次则以 grounding_failed 终止
                    if iterations >= self.max_iterations:
                        reason = "grounding_failed"
                        terminated = True
                        break
                    continue
                if tool_success_this_iter:
                    terminated = True
                    reason = "completed"
                    break
            else:
                # 本轮无工具调用
                if buffer.strip():
                    # 有文本输出即视为完成
                    terminated = True
                    reason = "completed"
                    break
                # 空输出则继续，依赖 max_iterations 防止无限循环
                continue

        # 收尾：未终止但已达轮次上限
        if not terminated and iterations >= self.max_iterations:
            reason = "max_iterations"
            terminated = True

        # 保证 token 计数与最终输出一致
        token_count = estimate_tokens(buffer)

        # 落盘用量信息（若有累计）
        try:
            if (_llm_usage_input or _llm_usage_output):
                _llm_usage_dict = {"input_tokens": int(_llm_usage_input), "output_tokens": int(_llm_usage_output)}
                if trace_writer is not None:
                    try:
                        trace_writer.append({"type": "llm_usage", "llm_usage": _llm_usage_dict})
                    except Exception:
                        pass
                # 将用量信息写入轨迹目录下的 llm_usage.json
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

        # 最终 grounding 状态：未配置则视为通过
        if self.grounding is None:
            grounding_verified = True

        # 解析轨迹路径
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

        # 从文本中提取指标（如 sharpe）
        if metrics is None:
            try:
                m = re.search(r"sharpe\s*[:=]?\s*([-\d\.]+)", buffer, re.IGNORECASE)
                if m:
                    metrics = {"sharpe": float(m.group(1))}
            except Exception:
                metrics = None

        # 成功路径保证 terminated 为 True，reason 区分具体终止原因

        # 最终壁时间可观测性上报
        try:
            _elapsed_final = time.monotonic() - _wall_start
            from hero_quant.metrics import observe_wall_time

            _status_final = "exceeded" if reason == "wall_time_budget_exceeded" else "success"
            observe_wall_time("agent_loop", float(_elapsed_final), status=_status_final)
            # 若收尾时才超限则覆盖 reason
            if _wall_budget is not None and _elapsed_final > float(_wall_budget) and reason != "wall_time_budget_exceeded":
                reason = "wall_time_budget_exceeded"
                terminated = True
                from hero_quant.metrics import inc_wall_time_exceeded

                inc_wall_time_exceeded("agent_loop")
                if trace_writer is not None:
                    try:
                        trace_writer.append({"type": "wall_time", "reason": "wall_time_budget_exceeded", "elapsed": float(_elapsed_final), "budget": float(_wall_budget)})
                    except Exception:
                        pass
        except Exception:
            pass

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

    def _run_graph(self, goal: str) -> LoopResult:
        """图委托路径，调用 research graph 并转为 LoopResult。"""

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
        # 调用图执行，兼容不同 invoke 签名
        result: Any = None
        try:
            state = {"messages": [{"role": "user", "content": goal}]}
            try:
                result = g.invoke(state)  # type: ignore[attr-defined]
            except TypeError:
                result = g.invoke(state, config={})  # type: ignore
        except BaseException as e:
            if trace_writer is not None:
                try:
                    trace_writer.append({"type": "graph_error", "error": str(e)})
                except Exception:
                    pass
            return LoopResult(terminated=True, iterations=1, text=f"graph_error: {e}", reason="graph_error", token_count=len(str(e)))

        # 将图结果转为文本
        text = ""
        try:
            if isinstance(result, dict):
                msgs = result.get("messages") or result.get("msgs") or []
                if isinstance(msgs, list) and msgs:
                    # 取最后一条消息内容
                    last = msgs[-1]
                    if isinstance(last, dict):
                        text = str(last.get("content", "") or last.get("text", "") or "")
                    else:
                        text = str(last)
                else:
                    text = str(result)
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
        # 图结果的接地性校验：若包含价格则尝试校验
        grounding_verified = True
        if self.grounding is not None and text:
            try:
                nums = re.findall(r"\d+\.?\d*", text)
                if nums:
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

        # 提取指标
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

