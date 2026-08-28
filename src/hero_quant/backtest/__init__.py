"""回测包：导出 BacktestEngine、批量与指标、校验能力。

职责：对外统一暴露 backtest 核心能力，保持导入路径稳定。
"""

from types import MappingProxyType

from .bench import DEFAULT_BENCHMARK_MAP as _DEFAULT_BENCHMARK_MAP_RAW, _resolve_benchmark, run_batch
from .engine import BacktestEngine
from .metrics import compute_metrics, sharpe_ratio, max_drawdown, annual_return, turnover

# 保护全局可变映射：对外只暴露只读视图，调用方 mutation 不影响模块全局
DEFAULT_BENCHMARK_MAP = MappingProxyType(dict(_DEFAULT_BENCHMARK_MAP_RAW))

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
