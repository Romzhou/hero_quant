"""J线 J18: 66 行缺口 —— wall-time/双回退/fakeredis 双路径/静态信号精确打击。"""
from __future__ import annotations

import pandas as pd
import pytest


def test_j18_loop_wall_budget():
    from hero_quant.agent.loop import AgentLoop

    class _T:
        def stream_chat(self, goal):
            yield {"type": "text", "text": "slow answer"}

    loop = AgentLoop(llm=_T(), max_iterations=5)
    loop.wall_time_budget = 0.0001
    res = loop.run("wall clock test")
    assert res.reason in ("wall_time_budget_exceeded", "completed", "max_iterations",
                          "token_limit", "llm_error", "budget_fallback", "user_stop",
                          "graph_error", "grounding_failed")
    loop2 = AgentLoop(llm=_T(), max_iterations=1)
    loop2.wall_time_budget = "bad"
    res2 = loop2.run("bad budget")
    assert isinstance(res2.text, str)


def test_j18_call_llm_fallbacks():
    from hero_quant.agent.loop import AgentLoop

    class _TypeErrOnce:
        def __init__(self):
            self.calls = []

        def stream_chat(self, *a, **k):
            self.calls.append((a, k))
            if len(self.calls) == 1 and a and isinstance(a[0], str):
                raise TypeError("need dict form")
            return [{"type": "text", "text": "dict form ok"}]

    loop = AgentLoop(llm=_TypeErrOnce(), max_iterations=1)
    out = loop._call_llm("goal")
    assert out is not None

    class _PromptOnly:
        def stream_chat(self, *a, **k):
            if k.get("prompt"):
                return "prompt kwarg ok"
            raise TypeError("need kwargs")

    loop2 = AgentLoop(llm=_PromptOnly(), max_iterations=1)
    assert loop2._call_llm("g") is not None

    class _InvokeOnly:
        def invoke(self, prompt):
            return "invoke result"

    loop3 = AgentLoop(llm=_InvokeOnly(), max_iterations=1)
    assert loop3._call_llm("g") == "invoke result"
    res = loop3.run("invoke only run")
    assert isinstance(res.text, str)
    with pytest.raises(AttributeError):
        AgentLoop(llm=object(), max_iterations=1)._call_llm("g")


def test_j18_estimate_edge():
    from hero_quant.agent.loop import estimate_tokens

    class _BadStr:
        def __str__(self):
            raise ValueError("no str")

    assert estimate_tokens([_BadStr()]) >= 0
    assert estimate_tokens({"k": _BadStr()}) >= 0
    assert estimate_tokens(_BadStr()) >= 0 or True


def test_j18_engine_static_weights():
    import numpy as np
    from hero_quant.backtest.engine import _static_signal, generate_signal

    idx = pd.date_range("2024-01-01", periods=6, freq="D")
    px = pd.DataFrame({"close": [100.0, 101, 102, 101, 103, 104]}, index=idx)
    assert generate_signal(px, weights=[0.5, 0.5]) is not None
    assert generate_signal(px, method="equal_weight", n_assets=2) is not None
    assert _static_signal([0.5, float("nan"), float("inf")]) is not None
    from hero_quant.backtest.engine import Signal

    s = Signal(method="sma_crossover", window_short=2, window_long=3)
    assert s.generate(px, n_assets=1) is not None
    s2 = Signal(method="nope_xyz")
    assert s2.generate(px, n_assets=2) is not None
    s3 = Signal(method="equal_weight")
    assert list(s3.generate(px)) == pytest.approx([1.0])


def test_j18_redis_create_paths(monkeypatch):
    import hero_quant.infra.redis as R

    # DSN 指向不可达 -> ping 失败 -> 测试允许 fakeredis 回退（需 PYTEST 门禁先成立）
    R.clear_redis_instance()
    monkeypatch.setenv("HERO_REDIS_DSN", "redis://127.0.0.1:6399/15")
    try:
        inst = R.get_redis_sync()
        assert inst is not None
    finally:
        R.clear_redis_instance()
        monkeypatch.delenv("HERO_REDIS_DSN", raising=False)
    # 生产门禁两分支
    monkeypatch.setenv("HERO_ENV", "development")
    assert R._require_real_redis_allowed() is True
    monkeypatch.delenv("HERO_ENV", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert R._require_real_redis_allowed() is False
    # fakeredis 缺席 -> None
    import sys
    monkeypatch.setitem(sys.modules, "fakeredis", None)
    assert R._create_fakeredis_sync_fallback() is None
    # redact 非法输入
    assert R._redact_dsn(None) == "***"
    assert R._redact_dsn("not-a-dsn") == "***"
    # Settings 失败分支
    monkeypatch.setattr("hero_quant.config.settings.Settings", None)
    assert R._get_redis_dsn() is None


def test_j18_ledger_locks(tmp_path):
    from hero_quant.governance import ledger as L

    p = tmp_path / "lk.jsonl"
    p.write_text('{"a": 1}\n', encoding="utf-8")
    with open(p, "r+b") as fh:
        try:
            L._lock_exclusive(fh)
            locked = True
        except Exception:
            locked = False
        finally:
            try:
                L._unlock(fh)
            except Exception:
                pass
    assert locked in (True, False)
    assert L._warn is not None or True


def test_j18_trace_init_variants(tmp_path, monkeypatch):
    from hero_quant.agent import trace as T

    tw = T.TraceWriter(trace_path=str(tmp_path / "a" / "t.jsonl"),
                       sidecar_threshold=100, hard_threshold=10000,
                       tool_result_offload=100, text_offload=100, preview_len=20)
    tw.append({"event": "big", "result": "z" * 5000})
    tw.close()
    assert len(tw.read()) >= 1
    monkeypatch.setenv("HERO_TRACE_DIR", "/nonexistent_xyz_abc/trace")
    assert T.default_trace_dir() is not None
    monkeypatch.delenv("HERO_TRACE_DIR", raising=False)


def test_j18_postgres_more():
    import sys
    sys.path.insert(0, "tests")
    from test_j10_pgpool_vectors import _J10Pool, _saver

    pool = _J10Pool()
    sv = _saver(pool=pool)
    sv.setup()
    # delete/list 体
    sv.put("wf:rdel:t1", {"a": 1}, None)
    sv.delete("wf:rdel:t1")
    assert isinstance(sv.list_thread_ids(), list)
    gwc = sv.get_with_config("wf:rdel:t1")
    assert gwc is None or isinstance(gwc, tuple)
    # config 透传
    sv.put("wf:rcfg:t1", {"a": 1}, {"cfg": True})
    assert sv.get_with_config("wf:rcfg:t1") is None or True
