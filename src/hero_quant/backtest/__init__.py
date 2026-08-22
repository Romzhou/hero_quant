"""回测包：导出 BacktestEngine、批量与指标、校验能力。

职责：对外统一暴露 backtest 核心能力，保持导入路径稳定。
"""

from .bench import DEFAULT_BENCHMARK_MAP, _resolve_benchmark, run_batch
from .engine import BacktestEngine
from .metrics import compute_metrics, sharpe_ratio, max_drawdown, annual_return, turnover

__all__ = [
    "BacktestEngine",
    "compute_metrics",
    "sharpe_ratio",
    "max_drawdown",
    "annual_return",
    "turnover",
    "run_batch",
    "_resolve_benchmark",
    "DEFAULT_BENCHMARK_MAP",
]
