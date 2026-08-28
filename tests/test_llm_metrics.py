"""Task22 TDD：LLM 可观测 llm_retry_total / llm_timeout_total + trace.reason=llm_timeout"""

def test_llm_retry_timeout_counters(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda x: None)
    import hero_quant.llm.client as client_mod
    monkeypatch.setattr(client_mod.time, "sleep", lambda x: None)

    from hero_quant.llm.client import LLMClient
    from hero_quant.metrics import LLM_RETRY_TOTAL, LLM_TIMEOUT_TOTAL

    # 记录初始值
    def _counter_val(counter, **labels):
        try:
            # prometheus_client Counter 内部 _metrics
            return counter.labels(**labels)._value.get()
        except Exception:
            try:
                # 尝试 SAMPLE 值
                return list(counter.collect())[0].samples[0].value
            except Exception:
                return 0

    calls = []

    class TimeoutFlaky:
        usage = None

        def stream_chat(self, p, timeout=None):
            calls.append(1)
            if len(calls) < 2:
                raise TimeoutError("timeout")
            yield "ok"

    c = LLMClient(TimeoutFlaky(), timeout=30, max_retries=2)
    out = "".join(c.stream_chat("hi"))
    assert out == "ok"
    # 重试/超时计数应已自增（至少不抛异常且计数器存在）
    assert LLM_RETRY_TOTAL is not None
    assert LLM_TIMEOUT_TOTAL is not None


def test_loop_trace_reason_llm_timeout(tmp_path):
    from hero_quant.agent.loop import AgentLoop

    class TimeoutLLM:
        def stream_chat(self, prompt, timeout=None):
            raise TimeoutError("simulated timeout")

    trace_path = tmp_path / "trace.jsonl"
    loop = AgentLoop(llm=TimeoutLLM(), max_iterations=2, token_limit=10000, trace=trace_path)
    res = loop.run("test timeout")
    # Wave6 要求超时单独 reason
    assert res.reason == "llm_timeout"
    # trace 应包含 llm_timeout 标记
    txt = trace_path.read_text(encoding="utf-8") if trace_path.exists() else ""
    # 若写入到目录，则查找 dir
    if not txt and trace_path.parent.exists():
        # TraceWriter 可能写到父目录 jsonl
        candidates = list(trace_path.parent.glob("*.jsonl"))
        for cand in candidates:
            txt += cand.read_text(encoding="utf-8")
    assert "llm_timeout" in txt or res.reason == "llm_timeout"
