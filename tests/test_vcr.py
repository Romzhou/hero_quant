"""C1-2 llm_usage VCR 骨架 — TDD RED before impl.

Requires:
- mock LLM 返回 usage_metadata 时 trace.jsonl 含 llm_usage {input_tokens, output_tokens}
- replay 分支读 llm_usage.json 不调 LLM
"""
import json


def test_llm_usage_recorded_in_trace(tmp_path):
    from hero_quant.agent.trace import TraceWriter
    from hero_quant.agent.loop import AgentLoop

    trace_path = tmp_path / "trace.jsonl"
    writer = TraceWriter(trace_path)

    class FakeLLM:
        def stream_chat(self, goal):
            # two chunks with usage_metadata, should accumulate
            yield {"type": "text", "text": "hello ", "usage_metadata": {"input_tokens": 10, "output_tokens": 5}}
            yield {"type": "text", "text": "world", "usage_metadata": {"input_tokens": 5, "output_tokens": 7}}

    try:
        loop = AgentLoop(llm=FakeLLM(), trace=writer, max_iterations=2)
        result = loop.run("test vcr")
        assert "hello" in result.text, f"result.text should contain hello, got {result.text!r}"
    finally:
        writer.close()

    # trace.jsonl should contain llm_usage record
    lines = trace_path.read_text(encoding="utf-8").strip().splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    # find record with llm_usage
    usage_records = [r for r in records if "llm_usage" in r]
    assert usage_records, f"trace.jsonl missing llm_usage, records={records}"
    # accumulated tokens: input 15, output 12 — all records must converge to final totals
    for rec in usage_records:
        assert rec["llm_usage"]["input_tokens"] == 15, f"expected input_tokens 15 got {rec['llm_usage']}"
        assert rec["llm_usage"]["output_tokens"] == 12, f"expected output_tokens 12 got {rec['llm_usage']}"
    # exactly one final usage record (no per-chunk duplicates)
    # allow single final record; if more, they must all be final totals (covered above)

    # also llm_usage.json should be written for replay — explicit sibling path contract
    usage_json = trace_path.parent / "llm_usage.json"
    assert usage_json.exists(), "llm_usage.json should be written for VCR replay"
    data = json.loads(usage_json.read_text(encoding="utf-8"))
    # explicit key check (avoid falsy fallback)
    llm_usage = data["llm_usage"] if "llm_usage" in data else data
    assert llm_usage["input_tokens"] == 15
    assert llm_usage["output_tokens"] == 12


def test_replay_does_not_call_llm(tmp_path):
    from hero_quant.agent.trace import TraceWriter
    from hero_quant.agent.loop import AgentLoop

    # prepare a llm_usage.json that simulates a prior recording
    replay_dir = tmp_path / "replay_src"
    replay_dir.mkdir(parents=True, exist_ok=True)
    # write canonical replay file
    replay_payload = {
        "text": "replayed hello",
        "llm_usage": {"input_tokens": 3, "output_tokens": 4},
        # optional chunks for richer replay
        "chunks": [{"type": "text", "text": "replayed hello"}],
    }
    replay_file = replay_dir / "llm_usage.json"
    replay_file.write_text(json.dumps(replay_payload, ensure_ascii=False), encoding="utf-8")

    # destination trace for replay run
    dest_trace = tmp_path / "dest" / "trace.jsonl"
    dest_trace.parent.mkdir(parents=True, exist_ok=True)

    class ShouldNotBeCalled:
        def stream_chat(self, goal):
            raise AssertionError("LLM should not be called in replay mode")
            yield  # make it a generator

    # AgentLoop must support replay branch: reading llm_usage.json without calling LLM
    # Accept either replay=True + replay_path or replay_path alone
    writer = TraceWriter(dest_trace)
    try:
        try:
            loop = AgentLoop(llm=ShouldNotBeCalled(), trace=writer, replay_path=str(replay_file))
        except TypeError as e:
            if "replay_path" not in str(e) and "replay" not in str(e).lower():
                raise
            # fallback: replay alias
            loop = AgentLoop(llm=ShouldNotBeCalled(), trace=writer, replay=True, replay_from=str(replay_file))

        result = loop.run("test replay")
    finally:
        writer.close()

    # result should contain replayed text and not have called LLM
    assert "replayed hello" in result.text, f"replayed text missing, got {result.text}"
    assert result.terminated == True  # noqa: E712 — allow truthy but prefer == True over `is True`

    # trace should still contain llm_usage
    lines = dest_trace.read_text(encoding="utf-8").strip().splitlines()
    records = [json.loads(l) for l in lines if l.strip()]
    usage_records = [r for r in records if "llm_usage" in r]
    assert usage_records, f"replay trace missing llm_usage, records={records}"
    found = usage_records[0]["llm_usage"]
    assert found["input_tokens"] == 3
    assert found["output_tokens"] == 4
