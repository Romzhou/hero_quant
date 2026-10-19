"""L线性能基准：quantlib 指标 + 回测核心路径 (engine.run / bench.run_batch)。

可复现命令 (仓库根):
    python scripts/bench_quantlib.py
    python scripts/bench_quantlib.py --repeat 5 --json docs/bench_result.json

输出: 各 case 单次耗时 (mean/min/max, ms) + 吞吐 (rows/s)，stdout 人类可读表 + JSON。
合成数据 deterministic (seed 固定)，不触网、不读 .env、不落盘回测产物。
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time

import numpy as np
import pandas as pd


def _make_series(n: int, seed: int = 42) -> pd.Series:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.5, size=n))
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.Series(close, index=idx, name="close")


def _make_prices(n: int, seed: int = 42) -> pd.DataFrame:
    s = _make_series(n, seed)
    df = pd.DataFrame({"close": s.astype(float)}, index=s.index)
    df["open"] = df["close"].shift(1).fillna(df["close"].iloc[0])
    return df


def _bench_case(name: str, fn, repeat: int) -> dict:
    fn()  # warmup (import/numba-less, 纯 pandas 预热)
    durs: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        durs.append((time.perf_counter() - t0) * 1000.0)
    return {
        "case": name,
        "repeat": repeat,
        "mean_ms": round(statistics.mean(durs), 3),
        "min_ms": round(min(durs), 3),
        "max_ms": round(max(durs), 3),
        "stdev_ms": round(statistics.pstdev(durs), 3) if len(durs) > 1 else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="quantlib/回测核心路径性能基准")
    ap.add_argument("--repeat", type=int, default=5, help="每 case 重复次数")
    ap.add_argument("--n-small", type=int, default=1000, help="指标 case 序列长度")
    ap.add_argument("--n-big", type=int, default=5000, help="大序列 case 长度")
    ap.add_argument("--json", type=str, default="", help="可选: 结果 JSON 落盘路径")
    args = ap.parse_args()

    from hero_quant.quantlib import indicators as ind
    from hero_quant.backtest.engine import BacktestEngine
    from hero_quant.backtest import bench as bench_mod

    n_small, n_big, repeat = args.n_small, args.n_big, args.repeat
    s_small = _make_series(n_small)
    s_big = _make_series(n_big)
    eq = _make_series(n_small)

    cases: list[tuple[str, object]] = [
        (f"sma(n={n_small})", lambda: ind.sma(s_small, 20)),
        (f"ema(n={n_small})", lambda: ind.ema(s_small, 20)),
        (f"rsi(n={n_small})", lambda: ind.rsi(s_small, 14)),
        (f"bollinger(n={n_small})", lambda: ind.bollinger(s_small, 20)),
        (f"macd(n={n_small})", lambda: ind.macd(s_small)),
        (f"max_drawdown(n={n_small})", lambda: ind.max_drawdown(eq)),
        (f"sma(n={n_big})", lambda: ind.sma(s_big, 20)),
        (f"rsi(n={n_big})", lambda: ind.rsi(s_big, 14)),
        (f"macd(n={n_big})", lambda: ind.macd(s_big)),
    ]

    # 回测核心路径：单资产 engine.run (252 日 / 1000 日, allow_synthetic=True)
    prices_252 = _make_prices(252)
    prices_1000 = _make_prices(1000)

    def _engine_252():
        BacktestEngine().run(prices_252, allow_synthetic=True)

    def _engine_1000():
        BacktestEngine().run(prices_1000, allow_synthetic=True)

    def _batch_3x5d():
        bench_mod.run_batch(
            ["AAA", "BBB.SS", "CCC.HK"],
            dates=["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
            allow_synthetic=True,
        )

    cases.append(("engine.run(252d)", _engine_252))
    cases.append(("engine.run(1000d)", _engine_1000))
    cases.append(("bench.run_batch(3 tickers x 5d)", _batch_3x5d))

    results = [_bench_case(name, fn, repeat) for name, fn in cases]
    # 吞吐列：指标类按序列长度折算 rows/s
    for r in results:
        n = n_big if f"n={n_big}" in r["case"] else (n_small if "n=" in r["case"] else None)
        r["rows_per_s"] = round(n / (r["mean_ms"] / 1000.0)) if n else None

    machine = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "processor": platform.processor(),
        "cpu_count": __import__("os").cpu_count(),
        "pandas": pd.__version__,
        "numpy": np.__version__,
    }
    payload = {"machine": machine, "params": {"repeat": repeat, "n_small": n_small, "n_big": n_big}, "results": results}

    # 人类可读表
    print(f"# bench_quantlib (repeat={repeat}, n_small={n_small}, n_big={n_big})")
    print(f"# machine: {machine['platform']} | python {machine['python']} | pandas {machine['pandas']} | numpy {machine['numpy']}")
    print(f"{'case':38s} {'mean_ms':>10s} {'min_ms':>10s} {'max_ms':>10s} {'rows/s':>10s}")
    for r in results:
        rps = str(r["rows_per_s"]) if r["rows_per_s"] else "-"
        print(f"{r['case']:38s} {r['mean_ms']:10.3f} {r['min_ms']:10.3f} {r['max_ms']:10.3f} {rps:>10s}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print(f"# wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
