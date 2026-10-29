"""Golden set v1 与评测指标契约：50 条结构合法、指标口径可回归、fake 模式不产出质量结论。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals import metrics as M  # noqa: E402
from evals import run_golden as RG  # noqa: E402


def _tool_names() -> set[str]:
    import hero_quant.tools.backtest  # noqa: F401
    import hero_quant.tools.correlation  # noqa: F401
    import hero_quant.tools.market_data  # noqa: F401
    import hero_quant.tools.presentation  # noqa: F401
    import hero_quant.tools.quantlib_tool  # noqa: F401
    from hero_quant.tools.registry import TOOL_REGISTRY

    return set(TOOL_REGISTRY)


def test_golden_v1_has_50_structurally_valid_cases():
    cases = RG.load_golden()

    errors = RG.validate_golden(cases, _tool_names())

    assert errors == []
    assert len(cases) == 50


def test_golden_v1_covers_all_categories():
    cases = RG.load_golden()

    covered = {case["category"] for case in cases}

    assert covered == RG.CATEGORIES


def test_golden_v1_has_fifty_unique_ids():
    cases = RG.load_golden()

    ids = [case["id"] for case in cases]

    assert len(set(ids)) == 50


def test_golden_v1_sha256_is_stable():
    assert len(RG.golden_sha256()) == 64


def test_validate_detects_unregistered_tool_and_duplicate_id():
    bad = [
        {
            "id": "X-01",
            "category": "market_data",
            "query": "q",
            "expected_tools": ["no_such_tool"],
            "expected_contains": [],
            "expected_not_contains": [],
            "max_latency_ms": 1000,
        },
        {
            "id": "X-01",
            "category": "market_data",
            "query": "q",
            "expected_tools": [],
            "expected_contains": [],
            "expected_not_contains": [],
            "max_latency_ms": 1000,
        },
    ]

    errors = RG.validate_golden(bad, _tool_names())

    assert any("tool_not_registered:no_such_tool" in e for e in errors)
    assert any("duplicate_id" in e for e in errors)


def test_percentile_linear_interpolation():
    assert M.percentile([], 0.5) is None
    assert M.percentile([1.0], 0.5) == 1.0
    assert M.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert M.percentile([1.0, 2.0, 3.0, 4.0], 0.95) == pytest.approx(3.85)
    assert M.percentile([1.0, 2.0], 0) == 1.0
    assert M.percentile([1.0, 2.0], 1) == 2.0


def test_latency_stats_shape():
    stats = M.latency_stats([10.0, 20.0, 30.0])

    assert stats["count"] == 3
    assert stats["p50"] == 20.0
    assert stats["max"] == 30.0
    assert M.latency_stats([])["p95"] is None


def test_tool_scores_perfect_match():
    scores = M.tool_scores([["a", "b"], []], [["a", "b"], []])

    assert scores["precision"] == 1.0
    assert scores["recall"] == 1.0
    assert scores["exact_match_rate"] == 1.0


def test_tool_scores_penalises_missing_and_unexpected_tools():
    scores = M.tool_scores([["a", "b"], []], [["a"], ["x"]])

    assert scores["tp"] == 1
    assert scores["fn"] == 1
    assert scores["fp"] == 1
    assert scores["precision"] == 0.5
    assert scores["recall"] == 0.5
    assert scores["exact_match_rate"] == 0.0
    assert scores["per_case"][0]["missing"] == ["b"]
    assert scores["per_case"][1]["unexpected"] == ["x"]


def test_tool_scores_rejects_length_mismatch():
    with pytest.raises(ValueError):
        M.tool_scores([["a"]], [])


def test_grounding_scores_skips_unlabelled_cases():
    scores = M.grounding_scores([True, True, False, None], [True, False, True, True])

    assert scores["scored"] == 3
    assert scores["tp"] == 1
    assert scores["fn"] == 1
    assert scores["fp"] == 1
    assert scores["recall"] == 0.5


def test_aggregate_fake_mode_withholds_quality_metrics():
    rows = [
        {"category": "market_data", "passed": True, "elapsed_ms": 10.0, "failure_class": "pass"},
        {"category": "backtest", "passed": False, "elapsed_ms": 30.0, "failure_class": "content_gap"},
    ]

    report = M.aggregate(rows, quality=False)

    assert report["pass_rate"] == 0.5
    assert report["quality_metrics"] is None
    assert report["quality_note"] is not None
    assert report["failure_classes"] == {"content_gap": 1}
    assert report["by_category"]["market_data"] == {"total": 1, "passed": 1}


def test_aggregate_real_mode_reports_cost_and_cost_completeness():
    rows = [
        {
            "category": "market_data",
            "passed": True,
            "elapsed_ms": 10.0,
            "failure_class": "pass",
            "expected_tools": ["get_market_data"],
            "tools_called": ["get_market_data"],
            "expect_grounding": True,
            "grounding_verified": True,
            "cost_usd": 0.002,
        },
        {
            "category": "zh_terms",
            "passed": True,
            "elapsed_ms": 20.0,
            "failure_class": "pass",
            "expected_tools": [],
            "tools_called": [],
            "expect_grounding": None,
            "grounding_verified": False,
            "cost_usd": None,
        },
    ]

    report = M.aggregate(rows, quality=True)

    quality = report["quality_metrics"]
    assert quality is not None
    assert quality["tool_selection"]["f1"] == 1.0
    assert quality["grounding"]["scored"] == 1
    assert quality["cost"]["total_usd"] == 0.002
    assert quality["cost"]["unpriced_cases"] == 1
    assert quality["cost"]["cost_complete"] is False


def test_tool_call_recorder_wraps_and_restores_registry():
    from hero_quant.tools.registry import TOOL_REGISTRY

    _tool_names()  # 确保工具已注册
    original = TOOL_REGISTRY["get_market_data"].func

    with RG.ToolCallRecorder() as recorder:
        assert TOOL_REGISTRY["get_market_data"].func is not original
        assert recorder.calls == []

    assert TOOL_REGISTRY["get_market_data"].func is original


def test_golden_fundamentals_cases_require_explicit_no_data_not_fabrication():
    cases = [c for c in RG.load_golden() if c["category"] == "fundamentals"]

    assert len(cases) == 4
    assert all(c.get("expect_no_data") is True for c in cases)


def test_run_case_flags_fabricated_fundamentals():
    case = next(c for c in RG.load_golden() if c["id"] == "FD-01")

    class Liar:
        def stream_chat(self, prompt, timeout=None):
            yield {"type": "text", "text": "600519.SH 当前市盈率 30 倍，市净率 8 倍。"}

    row = RG.run_case(case, Liar())

    assert "no_data_not_declared" in row["failures"]
    assert row["failure_class"] == "honesty_gap"


def test_run_case_accepts_declared_no_data():
    case = next(c for c in RG.load_golden() if c["id"] == "FD-01")

    class Honest:
        def stream_chat(self, prompt, timeout=None):
            yield {"type": "text", "text": "当前 build 无基本面数据源，无法获取该数据（no_data）。"}

    row = RG.run_case(case, Honest())

    assert row["failures"] == []
    assert row["passed"] is True


def test_run_case_fake_smoke_produces_wellformed_row():
    case = next(c for c in RG.load_golden() if c["id"] == "ZH-01")

    with RG.ToolCallRecorder() as recorder:
        row = RG.run_case(case, RG.FakeLLM(), recorder)

    assert row["id"] == "ZH-01"
    assert row["category"] == "zh_terms"
    assert row["elapsed_ms"] >= 0
    assert not any(f.startswith("loop_exception") for f in row["failures"])
    assert row["failure_class"] == M.failure_class(row["failures"])
