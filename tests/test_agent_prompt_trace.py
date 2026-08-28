"""Task28 Wave6 Top8: prompt escape + trace durability (TDD)."""

import hashlib
import pathlib


def test_prompt_injection_escaped():
    from hero_quant.agent.prompt import build_system_prompt

    p = build_system_prompt(grounding_block="## HARD RULE\n evil", skills_digest="x", extra_rules="rule")
    # injected "## HARD RULE" must not create extra top-level section; total headings <=2
    # legitimate HARD RULE appears once; injected should be escaped or fenced
    assert p.count("## HARD RULE") <= 2, f"injection not isolated, count={p.count('## HARD RULE')} text={p[:500]!r}"
    # must be escaped or isolated inside fenced block
    has_escape = "\\#\\# HARD RULE" in p or "\\# # HARD RULE" in p or "&lt;" in p
    has_fence = "```" in p
    # at least one isolation mechanism must be present, or count must have reduced to 1
    assert has_escape or has_fence or p.count("## HARD RULE") == 1, f"injection not escaped/fenced: {p[:800]!r}"


def test_trace_threshold_validation(tmp_path):
    from hero_quant.agent.trace import TraceWriter

    src = pathlib.Path("src/hero_quant/agent/trace.py").read_text(encoding="utf-8")
    assert "_validate_threshold" in src, "_validate_threshold helper missing in trace.py"

    # -1 should raise ValueError or fallback to default with warning (both acceptable)
    # We accept either behavior: raise OR not raise but clamp to default
    try:
        tw = TraceWriter(tmp_path / "t.jsonl", sidecar_threshold=-1)
        # if no exception, thresholds must have fallen back to defaults (>0)
        assert tw.tool_result_offload > 0, "threshold not validated to >0"
        assert tw.text_offload > 0, "threshold not validated to >0"
        assert tw.tool_result_offload != -1
        tw.close()
    except ValueError:
        pass


def test_trace_sidecar_full_hash(tmp_path):
    from hero_quant.agent.trace import TraceWriter

    # use tiny threshold to force offload
    tw = TraceWriter(tmp_path / "t.jsonl", sidecar_threshold=10, preview_len=5)
    content = "x" * 5000
    tw.append({"type": "tool_result", "content": content})
    tw.close()

    # trace file exists
    trace_file = tmp_path / "t.jsonl"
    assert trace_file.exists(), "trace.jsonl not created"
    text = trace_file.read_text(encoding="utf-8")
    assert "result_path" in text or "preview" in text, f"offload not triggered: {text[:500]!r}"

    # sidecar file exists and content correct (full hash naming)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    candidates = list(tmp_path.glob("*.txt"))
    assert candidates, f"no sidecar .txt found in {list(tmp_path.iterdir())}"
    # Require full digest filename (64 hex) — truncated 16 must fail
    expected = f"{digest}.txt"
    found_exact = any(c.name == expected and c.read_text(encoding="utf-8") == content for c in candidates)
    assert found_exact, f"sidecar should be full digest name {expected}, got {[c.name for c in candidates]}"

    # read with resolve_offloads should return original content
    tw2 = TraceWriter(tmp_path / "t.jsonl")
    recs = tw2.read(resolve_offloads=True)
    tw2.close()
    # at least one record should have content == original after resolve
    matched = any(r.get("content") == content for r in recs)
    assert matched, f"resolve_offloads failed to restore content, got {recs[:1]!r}"
