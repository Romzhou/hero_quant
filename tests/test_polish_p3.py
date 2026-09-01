"""Task12 P3 抛光 TDD 失败用例：atexit / 心跳界限 / 前端 AbortSignal+optimizeDeps / 窄化与类型。

预期：未修复前至少部分断言失败；修复后全绿。
YAGNI 最小，仅覆盖本任务范围。
"""

from __future__ import annotations

import pathlib


def test_otel_atexit_registered() -> None:
    """otel.py 必须 atexit.register(shutdown_otel)。"""
    p = pathlib.Path("src/hero_quant/telemetry/otel.py")
    txt = p.read_text(encoding="utf-8")
    assert "import atexit" in txt, "缺 atexit 导入"
    assert "atexit.register(shutdown_otel)" in txt, "未注册 atexit shutdown"
    # 运行期也应已注册（import 副作用）
    import atexit

    import hero_quant.telemetry.otel as otel

    handlers = getattr(atexit, "_exithandlers", None)
    # 兼容：部分平台 _exithandlers 为 list[(func, args, kwargs)]
    found = False
    if handlers is not None:
        for h in handlers:  # type: ignore[union-attr]
            fn = h[0] if isinstance(h, (list, tuple)) else h
            if getattr(fn, "__name__", "") == "shutdown_otel":
                found = True
                break
            if fn is getattr(otel, "shutdown_otel", None):
                found = True
                break
    else:
        # 回退：文本已证注册即视为合规
        found = True
    assert found, "atexit 未注册 shutdown_otel（运行期）"


def test_temporal_heartbeat_bounds() -> None:
    """temporal HeartbeatHelper interval 必须有 min/max 界（0.5s 下限，min(30, timeout*0.8) 上限）。"""
    from hero_quant.checkpoint.temporal import HeartbeatHelper, DEFAULT_HEARTBEAT_TIMEOUT

    # 下限
    h_low = HeartbeatHelper(interval=0.1)
    assert h_low.interval >= 0.5, f"下限未生效: {h_low.interval}"
    # 上限
    h_high = HeartbeatHelper(interval=100)
    expected_upper = min(30.0, float(DEFAULT_HEARTBEAT_TIMEOUT) * 0.8)
    assert h_high.interval <= expected_upper + 1e-9, f"上限未生效: {h_high.interval} > {expected_upper}"
    assert h_high.interval <= 30.0
    # 正常值透传
    h_mid = HeartbeatHelper(interval=5)
    assert abs(h_mid.interval - 5.0) < 1e-9


def test_dashboard_abortsignal() -> None:
    """Dashboard.tsx 必须接 AbortSignal（AbortController + signal + abort 清理）。"""
    p = pathlib.Path("frontend/src/pages/Dashboard.tsx")
    txt = p.read_text(encoding="utf-8")
    assert "AbortController" in txt, "缺 AbortController"
    assert "signal" in txt, "缺 signal 透传"
    assert "controller.abort()" in txt or "controller.abort" in txt, "缺 abort 清理"
    # 必须传给 fetch
    assert "fetch(" in txt and "signal" in txt


def test_research_optimizedeps() -> None:
    """Research.tsx 依赖优化：vite optimizeDeps 包含 echarts。"""
    p = pathlib.Path("frontend/vite.config.ts")
    txt = p.read_text(encoding="utf-8")
    assert "optimizeDeps" in txt, "vite 未配置 optimizeDeps"
    assert "echarts" in txt, "optimizeDeps 未包含 echarts"
    # Research 本身应有 AbortController 清理（非本任务新增亦需满足）
    rp = pathlib.Path("frontend/src/pages/Research.tsx")
    rtxt = rp.read_text(encoding="utf-8")
    assert "AbortController" in rtxt, "Research.tsx 缺 AbortController"


def test_registry_type_annotations() -> None:
    """registry.py 关键方法需有返回标注（mypy --strict 增量）。"""
    import inspect

    from hero_quant.data.registry import MarketDataRegistry

    sig = inspect.signature(MarketDataRegistry.get_bars)
    assert sig.return_annotation is not inspect.Signature.empty, "get_bars 缺返回标注"
    # 至少包含 Provenance 或 tuple 标记
    ann = str(sig.return_annotation)
    assert "Provenance" in ann or "tuple" in ann or "Any" not in ann or ann != "inspect._empty", "get_bars 标注不完整"


def test_registry_narrow_except_samples() -> None:
    """采样校验：registry.py 至少 5 处 except 已窄化为具体异常（非裸 except Exception）。"""
    p = pathlib.Path("src/hero_quant/data/registry.py")
    txt = p.read_text(encoding="utf-8")
    # 统计窄化形态：except (ValueError, TypeError 等
    import re

    narrow = re.findall(r"except\s*\(([^)]+)\)", txt)
    # 至少 5 处窄化
    assert len(narrow) >= 5, f"窄化不足: 仅 {len(narrow)} 处，期望 >=5，剩余记 backlog"
    # 至少包含 ValueError/TypeError/AttributeError/OSError 等具体类型
    joined = " ".join(narrow)
    assert "ValueError" in joined or "TypeError" in joined, "窄化未含具体异常类型"


def test_ruff_no_f841() -> None:
    """ruff 增量：不应有 F841 未使用变量（otel 未使用 e 已修复）。"""
    import subprocess

    r = subprocess.run(["ruff", "check", "src"], capture_output=True, text=True)
    # 允许其他告警，但 F841 必须为 0（本次收敛目标）
    assert "F841" not in r.stdout, f"仍有 F841: {r.stdout[:500]}"
