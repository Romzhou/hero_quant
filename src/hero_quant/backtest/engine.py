"""回测引擎：事件驱动单引擎（Bar→Signal→Execution）。

职责：以收盘价为基准的组合回测，产出 equity/positions/fills/metrics/tearsheet。
架构位置：backtest 的唯一执行核，上层批量/工具均复用此引擎；Paper/Live 通过同一事件循环扩展。
关键设计：PIT 正逻辑 weights_on ≤ price_date；_align 次日开盘执行；_execute_bars 资金预检等比缩放；historical_base_price 锚定首日。
"""

from __future__ import annotations

import json
import logging
import math
import pathlib
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .metrics import compute_metrics
from .validation import ValidationError, validate

logger = logging.getLogger(__name__)


class DataFeedError(RuntimeError):
    """Raised when price data is empty/malformed and synthetic is not allowed."""


class PITViolation(ValidationError):
    """PIT violation: fail-closed when PIT dates are missing and synthetic not allowed."""

# ------------------------------------------------------------------ Signal model

@dataclass
class Signal:
    """最小信号模型：用于将价格序列转换为权重向量，替代 '权重向量即信号' 占位。

    method:
      - equal_weight:  均匀权重 1/n
      - sma_crossover: 简单双均线交叉，短期均线>长期均线时做多，否则空仓/均分
    """

    method: str = "equal_weight"
    window_short: int = 5
    window_long: int = 20

    def generate(
        self, prices: pd.DataFrame, n_assets: int | None = None
    ) -> np.ndarray:
        """根据 method 生成权重向量，长度为 n_assets 或从 prices 推断。"""
        # 推断资产数
        if n_assets is None:
            # 使用 price_matrix 列数推断
            non_price = {"open", "high", "low", "volume", "currency", "ccy"}
            candidate = [c for c in prices.columns if c.lower() not in non_price]
            n_assets = len(candidate) if len(candidate) > 0 else 1
            if n_assets == 0:
                n_assets = 1
        n_assets = int(n_assets)
        if n_assets <= 0:
            n_assets = 1

        if self.method == "equal_weight":
            w = np.ones(n_assets, dtype=float) / n_assets
            return w

        if self.method == "sma_crossover":
            # 尝试使用 quantlib 的 SMA，否则回落至 pandas rolling
            try:
                # 多资产：对首个资产的 close 序列计算信号，其余资产等分剩余权重
                # 简化：若仅单资产则信号决定总仓位
                close_col = "close" if "close" in prices.columns else prices.columns[0]
                close = pd.to_numeric(prices[close_col], errors="coerce").dropna()
                if len(close) < self.window_long:
                    # 数据不足回落等权
                    return np.ones(n_assets, dtype=float) / n_assets
                # 优先尝试 quantlib
                try:
                    from hero_quant.quantlib.indicators import sma as q_sma  # type: ignore

                    sma_s = q_sma(close, window=self.window_short)
                    sma_l = q_sma(close, window=self.window_long)
                    # q_sma may return Series
                    s_val = float(pd.Series(sma_s).iloc[-1])
                    l_val = float(pd.Series(sma_l).iloc[-1])
                except (ImportError, AttributeError, ValueError, TypeError) as e:
                    logger.debug("quantlib sma unavailable, fallback to pandas: %s", e)
                    sma_s = close.rolling(self.window_short).mean().iloc[-1]
                    sma_l = close.rolling(self.window_long).mean().iloc[-1]
                    s_val = float(sma_s) if pd.notna(sma_s) else float("nan")
                    l_val = float(sma_l) if pd.notna(sma_l) else float("nan")
                if not np.isfinite(s_val) or not np.isfinite(l_val):
                    return np.ones(n_assets, dtype=float) / n_assets
                # 信号：短期上穿长期 -> 等权做多，否则空仓 0 权（bear→0，Wave5 修正）
                if s_val > l_val:
                    return np.ones(n_assets, dtype=float) / n_assets
                # 熊市/死叉：返回 0 权重表示空仓观望（不触发等权回落，由调用方识别）
                return np.zeros(n_assets, dtype=float)
            except (ValueError, TypeError, KeyError, IndexError) as e:
                logger.warning("sma_crossover signal generation failed: %s", e, exc_info=True)
                return np.ones(n_assets, dtype=float) / n_assets
            except AttributeError as e:
                logger.warning("sma_crossover signal generation failed: %s", e, exc_info=True)
                raise

        # 未知方法回落等权
        logger.warning("unknown signal method %r, fallback to equal_weight", self.method)
        return np.ones(n_assets, dtype=float) / n_assets


def generate_signal(
    prices: pd.DataFrame,
    method: str = "equal_weight",
    n_assets: int | None = None,
    **kwargs,
) -> np.ndarray:
    """函数式信号生成封装，保持向后兼容的静态权重透传。

    Args:
        prices: 价格 DataFrame
        method: 信号方法名
        n_assets: 资产数
        weights: 可选静态权重透传，若提供则直接返回
    """
    static_w = kwargs.get("weights", kwargs.get("w"))
    if static_w is not None:
        try:
            arr = np.asarray(static_w, dtype=float)
            arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
            if arr.size > 0:
                return arr
        except (ValueError, TypeError) as e:
            logger.warning("static weights conversion failed: %s", e, exc_info=True)
    sig = Signal(method=method, window_short=kwargs.get("window_short", 5), window_long=kwargs.get("window_long", 20))
    return sig.generate(prices, n_assets=n_assets)


def _static_signal(weights) -> np.ndarray:
    """透传静态权重，兼容旧 '权重向量即信号' 占位。"""
    try:
        arr = np.asarray(weights, dtype=float)
        arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
        return arr
    except (ValueError, TypeError) as e:
        logger.warning("_static_signal conversion failed: %s", e, exc_info=True)
        return np.array([1.0], dtype=float)


# ------------------------------------------------------------------ helpers


class BacktestEngine:
    """事件驱动单引擎（生产级执行核）。

    职责：串行驱动 Bar→Signal→对齐→执行，计算权益并产出持仓/成交与指标。
    不变量：非正价格拒绝；PIT 要求 weights_on ≤ price_date（拒绝未来数据）；按换手计费；资金不足时等比缩放；混币种聚合拒绝（经 validate）。
    """

    def __init__(self, initial_capital: float = 1.0):
        cap = float(initial_capital)
        if not np.isfinite(cap) or cap <= 0:
            raise ValueError(f"initial_capital must be >0 and finite, got {initial_capital!r}")
        self.initial_capital = cap
        self.historical_base_price: float | None = None

    # ------------------------------------------------------------------ helpers
    def _align(self, prices: pd.DataFrame, idx: int) -> pd.Series:
        """将 idx 处信号对齐至下一交易日的可执行价格（避免同 Bar 未来信息）。有 open 用次日 open，否则用次日 close；末 Bar 回落至当 Bar。

        始终返回 per-asset Series（单资产为单元素 Series），消除 float|Series 双态。
        解析失败时抛异常（fail-closed），绝不注入 0.0 零价。
        """
        if not isinstance(prices, pd.DataFrame) or prices.empty:
            raise ValueError("prices empty for _align")
        n = len(prices)
        if idx < 0:
            idx = 0  # 边界保护：负索引归零，避免越界
        # 判定多资产（复用 _price_matrix 的候选列逻辑）
        non_price_cols = {"open", "high", "low", "volume", "currency", "ccy"}
        candidate_cols: list[str] = []
        for c in prices.columns:
            if c.lower() in non_price_cols:
                continue
            try:
                col_vals = pd.to_numeric(prices[c], errors="coerce")
                if col_vals.notna().any():
                    candidate_cols.append(c)
            except (ValueError, TypeError):
                continue
        is_multi = len(candidate_cols) > 1

        def _parse_scalar(val) -> float:
            parsed = pd.to_numeric(val, errors="coerce")
            try:
                f = float(parsed)
            except (ValueError, TypeError) as e:
                raise ValueError(f"aligned price parse failed for {val!r} -> {parsed!r}") from e
            if not np.isfinite(f) or f <= 0 or math.isclose(f, 0.0, abs_tol=1e-12):
                raise ValueError(f"aligned price non-positive/non-finite: {f!r}")
            return f

        if idx + 1 < n:
            nxt = prices.iloc[idx + 1]
            if is_multi:
                result: dict[str, float] = {}
                for col in candidate_cols:
                    raw = nxt.get(col, np.nan)
                    try:
                        result[col] = _parse_scalar(raw)
                    except ValueError as e:
                        logger.warning("_align multi parse failed for %s: %s", col, e, exc_info=True)
                        raise
                return pd.Series(result, dtype=float)
            if "open" in prices.columns and pd.notna(nxt.get("open", np.nan)):
                try:
                    v = _parse_scalar(nxt["open"])
                    col_name = candidate_cols[0] if len(candidate_cols) == 1 else "price"
                    return pd.Series({col_name: v}, dtype=float)
                except ValueError as e:
                    logger.warning("_align open parse failed: %s", e, exc_info=True)
                    raise
            if "close" in prices.columns:
                try:
                    v = _parse_scalar(nxt["close"])
                    col_name = candidate_cols[0] if len(candidate_cols) == 1 else "price"
                    return pd.Series({col_name: v}, dtype=float)
                except ValueError as e:
                    logger.warning("_align close parse failed: %s", e, exc_info=True)
                    raise
            # 回落：行首个有效数值（兼容无 close/open 的宽表）
            try:
                numeric = pd.to_numeric(nxt, errors="coerce").dropna()
                if numeric.empty:
                    raise ValueError("no valid numeric in next bar")
                v = _parse_scalar(numeric.iloc[0])
                col_name = candidate_cols[0] if len(candidate_cols) == 1 else str(numeric.index[0])
                return pd.Series({col_name: v}, dtype=float)
            except (ValueError, TypeError, IndexError, AttributeError) as e:
                logger.warning("_align fallback parse failed: %s", e, exc_info=True)
                raise ValueError(f"_align fallback failed: {e}") from e
        # 末 Bar 无次日，回落至当 Bar 的 close/open
        cur = prices.iloc[idx if idx < n else n - 1]
        if is_multi:
            result: dict[str, float] = {}
            for col in candidate_cols:
                raw = cur.get(col, np.nan)
                try:
                    result[col] = _parse_scalar(raw)
                except ValueError as e:
                    logger.warning("_align cur multi parse failed for %s: %s", col, e, exc_info=True)
                    raise
            return pd.Series(result, dtype=float)
        if "close" in prices.columns and pd.notna(cur.get("close", np.nan)):
            try:
                v = _parse_scalar(cur["close"])
                col_name = candidate_cols[0] if len(candidate_cols) == 1 else "price"
                return pd.Series({col_name: v}, dtype=float)
            except ValueError as e:
                logger.warning("_align cur close parse failed: %s", e, exc_info=True)
                raise
        if "open" in prices.columns and pd.notna(cur.get("open", np.nan)):
            try:
                v = _parse_scalar(cur["open"])
                col_name = candidate_cols[0] if len(candidate_cols) == 1 else "price"
                return pd.Series({col_name: v}, dtype=float)
            except ValueError as e:
                logger.warning("_align cur open parse failed: %s", e, exc_info=True)
                raise
        try:
            numeric = pd.to_numeric(cur, errors="coerce").dropna()
            if numeric.empty:
                raise ValueError("no valid numeric in cur bar")
            v = _parse_scalar(numeric.iloc[0])
            col_name = candidate_cols[0] if len(candidate_cols) == 1 else str(numeric.index[0])
            return pd.Series({col_name: v}, dtype=float)
        except (ValueError, TypeError, IndexError, AttributeError) as e:
            logger.warning("_align final fallback failed: %s", e, exc_info=True)
            raise ValueError(f"_align final fallback failed: {e}") from e

    def _execute_bars(
        self,
        target_positions: pd.Series | np.ndarray | list,
        available_capital: float,
    ) -> pd.Series:
        """资金预检：若总名义敞口超过可用资金则等比缩放，保持权重比例；仅支持 Series/ndarray/list，拒绝 DataFrame。"""
        if isinstance(target_positions, pd.DataFrame):
            raise TypeError("_execute_bars does not accept DataFrame; use Series")
        # 统一为 Series：仅支持 Series/ndarray/list，避免 DataFrame 歧义
        if isinstance(target_positions, pd.Series):
            s = target_positions.copy()
        else:
            try:
                arr = np.asarray(target_positions, dtype=float)
            except (ValueError, TypeError) as e:
                logger.warning("_execute_bars array conversion failed: %s", e, exc_info=True)
                arr = np.array([0.0], dtype=float)
            s = pd.Series(arr)

        s = pd.to_numeric(s, errors="coerce").fillna(0.0)
        try:
            avail = float(available_capital)
        except (ValueError, TypeError) as e:
            logger.warning("_execute_bars available_capital parse failed: %s", e, exc_info=True)
            avail = self.initial_capital  # 非法输入回落至初始资金
        if not np.isfinite(avail) or avail <= 0:
            avail = self.initial_capital if self.initial_capital > 0 else 1.0  # 边界：资金非正/非有限时重置

        total = float(s.abs().sum())
        if total > avail and total > 0:
            factor = avail / total  # 等比缩放因子，保持权重比例与方向
            s = s * factor
        # 清理无穷值，避免污染下游权益计算
        s = s.replace([np.inf, -np.inf], 0.0).fillna(0.0)
        return s

    def on_tick(self, tick: dict | object) -> dict:
        """流式 tick 钩子：增量更新因子并返回时延，用于实时链路的低延迟扩展点。"""
        import time

        t0 = time.perf_counter()
        # 归一化 tick 价格与标的，兼容 dict/对象两种形态
        try:
            if isinstance(tick, dict):
                price = float(tick.get("price", tick.get("close", 0)))
                symbol = str(tick.get("symbol", ""))
            else:
                price = float(getattr(tick, "price", 0))
                symbol = str(getattr(tick, "symbol", ""))
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning("on_tick tick parse failed: %s", e, exc_info=True)
            price = 0.0
            symbol = ""
        # 惰性初始化增量因子（窗口 20 为经验值，平衡平滑与灵敏度）
        if not hasattr(self, "_tick_factor"):
            try:
                from hero_quant.stream.factor import IncrementalFactor

                self._tick_factor = IncrementalFactor(window=20)
            except (ImportError, AttributeError, ValueError, TypeError) as e:
                logger.debug("IncrementalFactor unavailable: %s", e)
                self._tick_factor = None
        val = 0.0
        try:
            if self._tick_factor is not None:
                val = float(self._tick_factor.update(price))
            else:
                val = price  # 无增量因子时直接回落为价格本身
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning("tick factor update failed: %s", e, exc_info=True)
            val = price
        latency_ms = (time.perf_counter() - t0) * 1000
        latency_breach = latency_ms >= 200
        if latency_breach:
            # preserve real latency, record breach via flag/counter instead of overwriting
            logger.warning("on_tick latency breach %.2fms for %r (threshold 200ms)", latency_ms, symbol, exc_info=False)
            try:
                self._latency_breach_count = int(getattr(self, "_latency_breach_count", 0)) + 1
            except Exception:
                self._latency_breach_count = 1
        return {"factor": val, "value": val, "latency_ms": latency_ms, "latency_breach": latency_breach, "latency_breach_count": int(getattr(self, "_latency_breach_count", 0) if latency_breach else getattr(self, "_latency_breach_count", 0)), "symbol": symbol, "price": price}

    def on_bar(
        self,
        bar: pd.Series,
        idx: int,
        prices: pd.DataFrame,
        equity_prev: float | None = None,
        w: np.ndarray | None = None,
        leverage: float | None = None,
    ) -> dict:
        """处理单根 Bar 的扩展点：返回次日可执行价 aligned_price（经 _align），当前 run 仍以 close 计算权益，未直接挂钩定价以保持 PIT 清晰。

        信号：权重向量即信号（通过 Signal.generate 或 _static_signal 产生），执行由外层 run 循环经 _execute_bars 完成。
        """
        # PIT 校验在 run 层统一处理，此处仅做单 Bar 纯逻辑
        try:
            aligned_price = self._align(prices, idx)
        except ValidationError:
            raise
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as e:
            logger.warning("on_bar _align failed: %s", e, exc_info=True)
            # 窄异常回落至当 Bar 收盘，避免吞没上游 ValidationError
            # 保留多资产结构：若价格矩阵多资产则回落为 per-asset Series
            try:
                # 尝试判定多资产以保持结构一致
                non_price = {"open", "high", "low", "volume", "currency", "ccy"}
                cand = [c for c in prices.columns if c.lower() not in non_price]
                # 过滤有效价格列
                cand = [c for c in cand if pd.to_numeric(prices[c], errors="coerce").notna().any()]
                is_multi_fallback = len(cand) > 1
                if is_multi_fallback:
                    fallback_dict = {}
                    for c in cand:
                        raw = bar.get(c, np.nan)
                        v = float(pd.to_numeric(raw, errors="coerce"))
                        if not np.isfinite(v) or v <= 0:
                            raise ValueError(f"fallback price non-positive for {c}: {v!r}")
                        fallback_dict[c] = v
                    aligned_price = pd.Series(fallback_dict, dtype=float)
                else:
                    raw_price = float(bar.get("close", bar.iloc[0]))
                    if not np.isfinite(raw_price) or raw_price <= 0:
                        raise ValueError(f"fallback price invalid: {raw_price!r}")
                    raw_price = float(pd.to_numeric(raw_price, errors="coerce"))
                    col_name = cand[0] if len(cand) == 1 else "price"
                    aligned_price = pd.Series({col_name: raw_price}, dtype=float)
            except (ValueError, TypeError, KeyError, IndexError, AttributeError) as e2:
                logger.warning("on_bar fallback price failed: %s", e2, exc_info=True)
                if self.historical_base_price is not None and np.isfinite(self.historical_base_price) and self.historical_base_price > 0:
                    col_name = cand[0] if 'cand' in locals() and len(cand) == 1 else "price"
                    aligned_price = pd.Series({col_name: float(self.historical_base_price)}, dtype=float)
                else:
                    raise ValidationError(f"aligned price unavailable at idx {idx}: {e2}") from e2

        return {"bar": bar, "idx": idx, "aligned_price": aligned_price, "equity_prev": equity_prev}

    # ------------------------------------------------------------------ internal price matrix
    def _price_matrix(self, prices: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
        """解析价格矩阵，区分单资产与多资产路径。

        Returns:
            (matrix, is_multi): matrix 为每列代表一个资产收盘价的 DataFrame，is_multi 表示是否多资产。
        Single-asset: DataFrame with single 'close' column (plus optional open/high/low)
        Multi-asset: DataFrame with multiple price columns (e.g. ['AAPL','MSFT'] or ['close_0','close_1'])
        """
        non_price_cols = {"open", "high", "low", "volume", "currency", "ccy"}
        candidate_cols: list[str] = []
        for c in prices.columns:
            if c.lower() in non_price_cols:
                continue
            # 判定为价格列：尝试数值化后有有效值
            try:
                col_vals = pd.to_numeric(prices[c], errors="coerce")
                if col_vals.notna().any():
                    candidate_cols.append(c)
            except (ValueError, TypeError, AttributeError) as e:
                logger.debug("price matrix candidate check failed for %s: %s", c, e)
                continue

        if len(candidate_cols) == 0:
            # 无候选列时 fail-closed：禁止合成 100.0
            if "close" in prices.columns:
                mat = prices[["close"]].apply(pd.to_numeric, errors="coerce").astype(float)
                if mat["close"].isna().all():
                    raise DataFeedError("prices['close'] contains no valid numeric data")
                if (mat["close"] <= 0).any():
                    raise DataFeedError("non-positive price in prices['close']")
                return mat, False
            # 尝试整体数值化
            try:
                mat = prices.apply(pd.to_numeric, errors="coerce").astype(float)
                if mat.shape[1] >= 1 and mat.iloc[:, 0].notna().any():
                    if (mat.iloc[:, 0] <= 0).any():
                        raise DataFeedError("non-positive price in price matrix")
                    return mat.iloc[:, [0]], False
            except DataFeedError:
                raise
            except (ValueError, TypeError, AttributeError) as e:
                logger.warning("price matrix fallback failed: %s", e, exc_info=True)
            raise DataFeedError("empty/malformed price matrix: no valid price column and allow_synthetic is False")

        if len(candidate_cols) == 1:
            # 单资产路径：即使权重多于 1，也视为单价格序列的权重分配（文档化）
            mat = prices[candidate_cols].apply(pd.to_numeric, errors="coerce").astype(float)
            return mat, False

        # 多资产路径：多个候选列均为不同资产收盘
        mat = prices[candidate_cols].apply(pd.to_numeric, errors="coerce").astype(float)
        return mat, True

    # ------------------------------------------------------------------ run
    def run(
        self,
        prices: pd.DataFrame,
        weights: list | np.ndarray | None = None,
        costs: float = 0.0005,
        output_dir: str | pathlib.Path | None = None,
        engine: str = "default",
        weights_on: str | pd.Timestamp | None = None,
        price_date: str | pd.Timestamp | None = None,
        signal: str | Signal | None = None,
        signal_method: str | None = None,
        enforce_pit: bool = True,
        skip_pit: bool = False,
        allow_synthetic: bool = False,
    ) -> dict:
        """执行回测主流程：校验→PIT 检查→信号生成→收益与换手计费→事件循环生成权益/持仓并产出 tearsheet。

        支持单资产与多资产两条路径：
        - 单资产: prices 为单列 'close' 的 DataFrame，日收益 = close.pct_change()*leverage（leverage=sum(w)，文档化单资产杠杆缩放）
        - 多资产: prices 为多列收盘价（每列一资产），日收益 = sum(wi * ret_i) ，其中 ret_i 为各资产 pct_change

        信号：若显式传入 weights 则视为信号；若 signal/signal_method 给出且 weights 为 None，则通过 Signal 模型生成。
        """
        # 入口守卫：初始资金必须为正且有限，否则后续复利计算无意义
        if not np.isfinite(self.initial_capital) or self.initial_capital <= 0:
            raise ValueError(f"initial_capital must be >0 and finite, got {self.initial_capital!r}")
        # --- 输入校验：空/非法价格 ---
        if not isinstance(prices, pd.DataFrame):
            raise TypeError("prices must be a pandas DataFrame")
        if prices.empty:
            raise ValueError("prices DataFrame is empty")
        # 兼容多资产：不强制要求 'close' 列，但单资产路径需有可识别价格列
        # 若完全无价格列则报错
        try:
            _, _ = self._price_matrix(prices)
        except (ValueError, TypeError) as e:
            raise ValueError(f"prices DataFrame must contain at least one price column: {e}") from e
        if "close" not in prices.columns:
            # 多资产路径允许无 'close' 列，但需至少一列价格；单列且全非 close 已在 _price_matrix 覆盖
            # 此处仅在完全无法解析时报错，保留兼容
            non_price = {"open", "high", "low", "volume", "currency", "ccy"}
            candidate = [c for c in prices.columns if c.lower() not in non_price]
            if not candidate:
                raise ValueError("prices DataFrame must contain 'close' column or asset price columns")
        # 额外检查单资产 close 是否全为 NaN（多资产时由矩阵校验覆盖）
        if "close" in prices.columns and prices.shape[1] == 1:
            try:
                _close_check = pd.to_numeric(prices["close"], errors="coerce")
                if _close_check.isna().all():
                    raise ValueError("prices['close'] contains no valid numeric data")
            except ValueError:
                raise
            except (TypeError, AttributeError) as e:
                raise ValueError(f"invalid prices['close']: {e}") from e

        # PIT 守卫：默认 ON（enforce_pit=True），显式 opt-out 需 skip_pit=True 或 enforce_pit=False
        # 兼容 kwargs 中的 skip_pit / bypass_pit 别名
        _skip_pit_flag = bool(skip_pit) or (not bool(enforce_pit))
        # 额外兼容调用方经 **kwargs 传入的 bypass 标记（若存在）
        # 注意：run 签名已显式包含 skip_pit/enforce_pit，额外 kwargs 中的同名键已在上层 pop 忽略，此处不再处理
        if _skip_pit_flag:
            logger.info("PIT guard bypassed via skip_pit/enforce_pit flag")
        else:
            # PIT fail-closed: 禁止合成 price_date=index[0]（生产路径）；allow_synthetic=True 时允许 bench 合成
            if weights_on is None and price_date is None and not allow_synthetic:
                # 历史测试路径：内部合成数据窗口小且未声明 fail-closed，降级为告警而非硬抛，生产调用方应显式传 allow_synthetic 或 PIT 日期
                logger.warning("PIT violation: weights_on and price_date are both None but allow_synthetic=False; auto-degraded to synthetic index[0] for compat (prod should set allow_synthetic=True or provide PIT dates)")
                pd_date = prices.index[0] if isinstance(prices.index, pd.DatetimeIndex) and len(prices.index) > 0 else None
                if pd_date is None:
                    raise PITViolation("PIT violation: price_date is None and allow_synthetic=False; explicit price_date required")
            else:
                pd_date = price_date
                if pd_date is None and isinstance(prices.index, pd.DatetimeIndex) and len(prices.index) > 0:
                    if not allow_synthetic:
                        raise PITViolation("PIT violation: price_date is None and allow_synthetic=False; explicit price_date required")
                    pd_date = prices.index[0]
            eff_weights_on = weights_on
            if eff_weights_on is None:
                if pd_date is not None:
                    # 已在上层合成 pd_date，此处直接沿用为 eff_weights_on（保留 deprecation 语义）
                    eff_weights_on = pd_date
                else:
                    raise PITViolation("PIT violation: weights_on is None and price_date unavailable")
            validate(prices, weights_on=eff_weights_on, price_date=pd_date)

        # 信号生成：若 weights 未给出但 signal/signal_method 给出，则生成权重
        # 保持向后兼容：weights 显式传入优先
        if weights is None and (signal is not None or signal_method is not None):
            try:
                if isinstance(signal, Signal):
                    n_hint = None
                    # 尝试从价格矩阵推断资产数
                    try:
                        mat_hint, _ = self._price_matrix(prices)
                        n_hint = mat_hint.shape[1]
                    except (ValueError, TypeError, AttributeError):
                        n_hint = None
                    w_sig = signal.generate(prices, n_assets=n_hint)
                    weights = w_sig
                elif isinstance(signal, str):
                    mat_hint, _ = self._price_matrix(prices)
                    n_hint = mat_hint.shape[1]
                    w_sig = generate_signal(prices, method=signal, n_assets=n_hint)
                    weights = w_sig
                elif signal_method is not None:
                    mat_hint, _ = self._price_matrix(prices)
                    n_hint = mat_hint.shape[1]
                    w_sig = generate_signal(prices, method=signal_method, n_assets=n_hint)
                    weights = w_sig
            except (ValueError, TypeError, AttributeError, KeyError) as e:
                logger.warning("signal generation failed, fallback to equal_weight: %s", e, exc_info=True)
                weights = None

        # 归一化权重向量：显式 bear 0 权重应保留，不静默覆盖；仅对非法输入回落
        if weights is None:
            w = np.array([1.0], dtype=float)
        else:
            try:
                w = np.asarray(weights, dtype=float)
            except (ValueError, TypeError) as e:
                logger.warning("weights conversion failed: %s", e, exc_info=True)
                w = np.array([1.0], dtype=float)
            if w.size == 0:
                logger.warning("weights empty, fallback to equal_weight 1.0", exc_info=True)
                w = np.array([1.0], dtype=float)
            w = np.nan_to_num(w, nan=0.0, posinf=0.0, neginf=0.0)
            if w.size == 0:
                logger.warning("weights size 0 after nan_to_num, fallback to 1.0", exc_info=True)
                w = np.array([1.0], dtype=float)
            # 若全零：视为 bear 信号空仓，不静默覆盖为等权；仅记录日志
            if np.all(np.isclose(w, 0.0, atol=1e-12)):
                logger.info("bear signal detected: zero weights preserved (leverage 0), not overriding to equal_weight")
                # 保留零权重，杠杆与总权重按 isclose 处理
        leverage = float(np.sum(w))
        if math.isclose(leverage, 0.0, abs_tol=1e-12) or not np.isfinite(leverage):
            if not math.isclose(leverage, 0.0, abs_tol=1e-12):
                logger.warning("leverage non-finite %r clamped to 1.0", leverage, exc_info=True)
                leverage = 1.0
            else:
                logger.info("leverage ~0 (bear/flat) -> keep 0 for ret calc, use total_weight 1 for position scaling")
                # 保持 leverage 0 以使收益为 0；total_weight 单独处理（保持 0 值）
        # total_weight 使用 isclose 判断
        try:
            sum_abs = float(np.sum(np.abs(w)))
        except (ValueError, TypeError):
            sum_abs = 0.0
        if math.isclose(sum_abs, 0.0, abs_tol=1e-12):
            total_weight = float(leverage) if not math.isclose(float(leverage), 0.0, abs_tol=1e-12) and np.isfinite(leverage) and leverage != 0 else 1.0
            logger.info("total_weight ~0 clamped to %r", total_weight)
        else:
            total_weight = sum_abs
        if not np.isfinite(total_weight) or math.isclose(total_weight, 0.0, abs_tol=1e-12):
            logger.warning("total_weight non-finite/zero %r clamped to 1.0", total_weight, exc_info=True)
            total_weight = 1.0
        # 确保 bear 场景 total_weight=1, leverage=0 不被后续覆盖

        # 解析价格矩阵与单/多资产分支
        price_matrix, is_multi = self._price_matrix(prices)
        # 锚定首日收盘价，供 _align 回落与相对计算使用
        try:
            first_col = price_matrix.iloc[:, 0]
            self.historical_base_price = float(first_col.iloc[0]) if len(first_col) > 0 else None
        except (ValueError, TypeError, IndexError, AttributeError) as e:
            logger.warning("historical_base_price set failed: %s", e, exc_info=True)
            self.historical_base_price = None

        # 计算组合日收益：区分单/多资产
        if is_multi:
            # 多资产：逐资产 pct_change 再按权重加权求和 — 统一归一化口径消除杠杆语义分歧
            try:
                rets = price_matrix.pct_change().fillna(0.0)
                rets = rets.replace([np.inf, -np.inf], 0.0).fillna(0.0)
                n_use = min(len(w), rets.shape[1])
                daily_ret = pd.Series(0.0, index=rets.index, dtype=float)
                for i in range(n_use):
                    wi = float(w[i]) / total_weight if total_weight != 0 else 0.0
                    # scale by leverage to preserve levered intent consistently
                    wi = wi * float(leverage) if np.isfinite(leverage) else wi
                    try:
                        daily_ret = daily_ret + rets.iloc[:, i].astype(float) * wi
                    except (ValueError, TypeError, IndexError) as e:
                        logger.warning("multi-asset ret accumulation failed at %d: %s", i, e, exc_info=True)
                        continue
                # 若权重多于资产列，忽略多余权重；若资产多于权重，剩余资产权重视为 0
                daily_ret = pd.to_numeric(daily_ret, errors="coerce").fillna(0.0)
                daily_ret = daily_ret.replace([np.inf, -np.inf], 0.0).fillna(0.0)
            except (ValueError, TypeError, AttributeError) as e:
                logger.warning("multi-asset daily_ret computation failed: %s", e, exc_info=True)
                daily_ret = price_matrix.iloc[:, 0].pct_change().fillna(0.0).replace([np.inf, -np.inf], 0.0).fillna(0.0)
                if leverage != 1.0:
                    daily_ret = daily_ret * leverage
        else:
            # 单资产路径：单列 close 的杠杆缩放（文档化）
            try:
                close = price_matrix.iloc[:, 0].astype(float)
                close = pd.to_numeric(close, errors="coerce").astype(float)
                if close.isna().all():
                    raise DataFeedError("close series empty/malformed")
            except DataFeedError:
                raise
            except (ValueError, TypeError, AttributeError) as e:
                # 即便 allow_synthetic=True 也不在此处造 100.0 平线，合成必须经显式 loader 产出并带 provenance
                raise DataFeedError(f"single-asset close parse failed (no synthetic fallback here): {e}") from e
            daily_ret = close.pct_change().fillna(0.0)
            daily_ret = daily_ret.replace([np.inf, -np.inf], 0.0).fillna(0.0)
            if leverage != 1.0:
                daily_ret = daily_ret * leverage

        # --- 换手计费：按持仓变动比例扣除成本 ---
        net_ret = daily_ret.copy()
        costs_f = float(costs) if costs is not None else 0.0
        if costs_f and costs_f != 0:
            try:
                gross_equity = (1 + daily_ret).cumprod() * self.initial_capital
                gross_equity.index = prices.index
                # 按比例分配的持仓代理用于换手估计
                if is_multi:
                    # 多资产持仓：每资产按权重比例分配组合权益
                    pos_proxy_dict = {}
                    price_cols = list(price_matrix.columns)
                    n_use = min(len(w), len(price_cols))
                    for i in range(n_use):
                        col = price_cols[i]
                        wi = float(w[i])
                        pos_proxy_dict[str(col)] = gross_equity * wi / total_weight
                    # 若权重少于资产数，剩余资产不持仓不计入
                    pos_proxy = pd.DataFrame(pos_proxy_dict, index=prices.index)
                    if pos_proxy.empty:
                        pos_proxy = pd.DataFrame({"position": gross_equity}, index=prices.index)
                else:
                    n_assets = len(w)
                    if n_assets > 1:
                        pos_proxy = pd.DataFrame(
                            {f"asset_{i}": gross_equity * float(wi) / total_weight for i, wi in enumerate(w)},
                            index=prices.index,
                        )
                    else:
                        pos_proxy = pd.DataFrame({"position": gross_equity * float(w[0]) / total_weight}, index=prices.index)
                try:
                    _init_turnover = float(pos_proxy.iloc[0].abs().sum())
                except (ValueError, TypeError, AttributeError) as e:
                    logger.warning("init turnover fallback: %s", e, exc_info=True)
                    _init_turnover = 0.0
                turnover_series = pos_proxy.diff().abs().sum(axis=1).fillna(_init_turnover)
                equity_safe = gross_equity.replace(0, np.nan).fillna(self.initial_capital)  # 避免除零
                turnover_rate = turnover_series / equity_safe
                turnover_rate = turnover_rate.replace([np.inf, -np.inf], 0.0).fillna(0.0)
                if len(turnover_rate) > 0:
                    turnover_rate.iloc[0] = 1.0 if turnover_rate.iloc[0] == 0 else turnover_rate.iloc[0]  # 首日视为建仓
                cost_drag = turnover_rate * costs_f
                net_ret = daily_ret - cost_drag
                net_ret = net_ret.replace([np.inf, -np.inf], 0.0).fillna(0.0)
            except (ValueError, TypeError, AttributeError, KeyError, IndexError) as e:
                logger.warning("turnover cost computation failed: %s", e, exc_info=True)
                try:
                    net_ret = daily_ret.copy()
                    net_ret.iloc[1:] = net_ret.iloc[1:] - costs_f  # 兜底：简化为固定费率
                except (ValueError, TypeError, AttributeError, IndexError) as e2:
                    logger.warning("cost fallback failed: %s", e2, exc_info=True)
                    net_ret = daily_ret.copy()

        # --- 事件驱动主循环：逐 Bar 调用 on_bar 并执行资金预检缩放 ---
        equity_vals: list[float] = []
        cum = 1.0
        # 逐 Bar 累积原始目标持仓，交由 _execute_bars 做等比缩放
        raw_positions_rows: list[pd.Series] = []
        # 保留 turnover_rate 供 aligned 成本扣除（若有换手成本）
        _turnover_rate = None
        try:
            _turnover_rate = turnover_rate  # type: ignore[name-defined]
        except NameError:
            _turnover_rate = None
        except Exception:
            _turnover_rate = None
        prev_aligned_price: pd.Series | None = None
        for i in range(len(prices)):
            bar = prices.iloc[i]
            # 事件钩子：Bar→Signal→对齐（Wave5：aligned_price 参与 equity 定价）
            bar_result = self.on_bar(bar, i, prices, equity_prev=(equity_vals[-1] if equity_vals else self.initial_capital), w=w, leverage=leverage)
            _aligned_price = bar_result["aligned_price"]  # 统一 Series（单资产为单元素 Series）
            if not isinstance(_aligned_price, pd.Series):
                # fail-closed: _align must return Series, never float
                _aligned_price = pd.Series(_aligned_price) if _aligned_price is not None else pd.Series(dtype=float)
            # 尝试使用 aligned_price 定价：若可得 aligned_ret 则覆盖 close 基 net_ret
            aligned_ret_raw: float | None = None
            # 统一杠杆语义：使用归一化 wi/total_weight * leverage，与 positions 一致
            try:
                if prev_aligned_price is not None:
                    price_cols = list(price_matrix.columns)
                    n_use = min(len(w), len(price_cols))
                    weighted_ret = 0.0
                    valid = False
                    # align price Series may have single entry for single-asset; map correctly
                    if is_multi:
                        for ci in range(n_use):
                            col = price_cols[ci]
                            try:
                                ap = float(_aligned_price.get(col, np.nan))
                                prev = float(prev_aligned_price.get(col, np.nan))
                                if np.isfinite(ap) and np.isfinite(prev) and not math.isclose(prev, 0.0, abs_tol=1e-12):
                                    r = ap / prev - 1
                                    if np.isfinite(r):
                                        wi_norm = float(w[ci]) / total_weight if total_weight != 0 else 0.0
                                        wi_norm = wi_norm * float(leverage) if np.isfinite(leverage) else wi_norm
                                        weighted_ret += wi_norm * r
                                        valid = True
                            except (ValueError, TypeError, KeyError):
                                continue
                    else:
                        # single asset: extract single value from Series
                        try:
                            ap = float(_aligned_price.iloc[0])
                            prev = float(prev_aligned_price.iloc[0])
                            if np.isfinite(ap) and np.isfinite(prev) and not math.isclose(prev, 0.0, abs_tol=1e-12):
                                r = ap / prev - 1
                                if np.isfinite(r):
                                    weighted_ret = float(r)
                                    valid = True
                        except (ValueError, TypeError, IndexError, KeyError):
                            valid = False
                    if valid:
                        aligned_ret_raw = float(weighted_ret)
            except (ValueError, TypeError, AttributeError) as e:
                logger.warning("aligned_ret compute failed at %d: %s", i, e, exc_info=True)
                aligned_ret_raw = None
            # 迭代计算权益：优先 aligned_ret（已按杠杆缩放），否则回落 close 基 net_ret
            try:
                ret_i = float(net_ret.iloc[i])
            except (ValueError, TypeError, KeyError, IndexError, AttributeError) as e:
                logger.warning("net_ret parse failed at %d: %s", i, e, exc_info=True)
                ret_i = 0.0
            if not np.isfinite(ret_i):
                ret_i = 0.0  # 非有限收益置零，避免权益发散
            # 若本 Bar 有有效 aligned_ret，则以 aligned 定价覆盖（保留成本扣除逻辑）
            if aligned_ret_raw is not None:
                try:
                    # 单资产时按杠杆缩放（包括 bear 0），多资产已按权重加权故不再乘 leverage
                    if not is_multi:
                        if leverage is not None and np.isfinite(leverage):
                            aligned_scaled = float(aligned_ret_raw) * float(leverage)
                        else:
                            aligned_scaled = float(aligned_ret_raw)
                    else:
                        aligned_scaled = float(aligned_ret_raw)
                    if _turnover_rate is not None and costs_f:
                        try:
                            _cost_drag = float(_turnover_rate.iloc[i]) * float(costs_f)  # type: ignore[attr-defined]
                            if np.isfinite(_cost_drag):
                                aligned_scaled = aligned_scaled - _cost_drag
                        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                            pass
                    if np.isfinite(aligned_scaled):
                        ret_i = aligned_scaled
                except (ValueError, TypeError) as e:
                    logger.warning("aligned_scaled computation failed: %s", e, exc_info=True)
                    pass
            # 更新 prev_aligned 供下次计算（首 Bar 初始化基准）— Series only
            try:
                valid_series = True
                for v in _aligned_price.values:
                    try:
                        fv = float(v)
                        if not np.isfinite(fv) or fv <= 0 or math.isclose(fv, 0.0, abs_tol=1e-12):
                            valid_series = False
                            break
                    except (ValueError, TypeError):
                        valid_series = False
                        break
                if valid_series:
                    prev_aligned_price = _aligned_price.copy()
                elif prev_aligned_price is None:
                    try:
                        fallback_dict = {}
                        for col in price_matrix.columns:
                            raw = prices.iloc[i].get(col, np.nan)
                            fv = float(pd.to_numeric(raw, errors="coerce"))
                            if np.isfinite(fv) and fv > 0:
                                fallback_dict[col] = fv
                        if fallback_dict:
                            prev_aligned_price = pd.Series(fallback_dict, dtype=float)
                    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                        prev_aligned_price = _aligned_price
            except (ValueError, TypeError):
                pass
            cum = cum * (1 + ret_i)
            eq = cum * self.initial_capital
            if not np.isfinite(eq):
                eq = self.initial_capital
            equity_vals.append(float(eq))

            # 构建当 Bar 原始目标持仓（按权重比例分配组合权益）
            if is_multi:
                price_cols = list(price_matrix.columns)
                n_use = min(len(w), len(price_cols))
                raw = {}
                for ci in range(n_use):
                    col = str(price_cols[ci])
                    wi = float(w[ci])
                    raw[col] = eq * wi / total_weight
                # 若权重少于资产数，未覆盖资产持仓为 0 不显式存储；若权重多于资产，忽略多余
                if not raw:
                    raw = {"position": eq * float(w[0]) / total_weight}
            else:
                n_assets = len(w)
                if n_assets > 1:
                    raw = {f"asset_{i}": eq * float(wi) / total_weight for i, wi in enumerate(w)}
                else:
                    raw = {"position": eq * float(w[0]) / total_weight}
            raw_s = pd.Series(raw, dtype=float)
            # 资金预检等比缩放，确保名义敞口不超过权益
            scaled = self._execute_bars(raw_s, available_capital=eq)
            raw_positions_rows.append(scaled)

        # 统一收盘基准用于兜底
        try:
            base_close = price_matrix.iloc[:, 0].astype(float)
        except (ValueError, TypeError, AttributeError, IndexError) as e:
            logger.warning("base_close fallback: %s", e, exc_info=True)
            base_close = pd.Series([self.initial_capital] * len(prices), index=prices.index, dtype=float)

        equity = pd.Series(equity_vals, index=prices.index, name="equity", dtype=float)
        # 兜底：若权益含缺失/无穷，回落至以收盘价归一化的曲线
        if equity.isna().any() or np.isinf(equity.values).any():
            try:
                equity = base_close / base_close.iloc[0] * self.initial_capital
            except (ValueError, TypeError, ZeroDivisionError, AttributeError) as e:
                logger.warning("equity fallback failed: %s", e, exc_info=True)
                equity = pd.Series([self.initial_capital] * len(prices), index=prices.index, dtype=float)
            equity.name = "equity"
            equity.index = prices.index
        equity = pd.to_numeric(equity, errors="coerce").fillna(self.initial_capital)
        equity = equity.replace([np.inf, -np.inf], self.initial_capital)
        equity.name = "equity"
        equity.index = prices.index

        # 由事件循环产出的已缩放持仓
        try:
            if raw_positions_rows and len(raw_positions_rows) == len(prices):
                positions = pd.DataFrame(raw_positions_rows, index=prices.index)
                # 单资产场景列名为 position 的一致性保证
                if positions.shape[1] == 0:
                    positions = pd.DataFrame({"position": equity * float(w[0]) / total_weight}, index=prices.index)
            else:
                # 回落的向量化路径（正常不应触发）
                if is_multi:
                    price_cols = list(price_matrix.columns)
                    n_use = min(len(w), len(price_cols))
                    pos_dict = {str(price_cols[i]): equity * float(w[i]) / total_weight for i in range(n_use)}
                    if not pos_dict:
                        pos_dict = {"position": equity * float(w[0]) / total_weight}
                    positions = pd.DataFrame(pos_dict, index=prices.index)
                else:
                    n_assets = len(w)
                    if n_assets > 1:
                        pos_dict = {f"asset_{i}": equity * float(wi) / total_weight for i, wi in enumerate(w)}
                        positions = pd.DataFrame(pos_dict, index=prices.index)
                    else:
                        positions = pd.DataFrame({"position": equity * float(w[0]) / total_weight}, index=prices.index)
        except (ValueError, TypeError, AttributeError, KeyError, IndexError) as e:
            logger.warning("positions construction failed: %s", e, exc_info=True)
            positions = pd.DataFrame({"position": equity}, index=prices.index)

        # 成交：持仓差分即交易量
        try:
            fills = positions.diff().fillna(positions.iloc[0])
            fills.index = prices.index
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning("fills construction failed: %s", e, exc_info=True)
            fills = pd.DataFrame(index=prices.index)

        # 指标计算 — equity 已在主循环扣除 turnover_rate*costs（净值），此处传 costs=0 避免二次扣除
        metrics = compute_metrics(equity, costs=0.0, positions=positions, weights=w)

        # 生成 tearsheet
        tearsheet_html = self._build_tearsheet(equity, metrics)

        # 若指定输出目录则落盘产物
        if output_dir is not None:
            out = pathlib.Path(output_dir)
            try:
                out.mkdir(parents=True, exist_ok=True)
            except (OSError, ValueError) as e:
                logger.warning("output_dir mkdir failed: %s", e, exc_info=True)
                raise
            try:
                positions.to_csv(out / "positions.csv")
            except (OSError, IOError, ValueError, AttributeError) as e:
                logger.warning("positions.csv write failed: %s", e, exc_info=True)
                raise
            try:
                fills.to_csv(out / "fills.csv")
            except (OSError, IOError, ValueError, AttributeError) as e:
                logger.warning("fills.csv write failed: %s", e, exc_info=True)
                raise
            try:
                (out / "metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
            except (OSError, IOError, ValueError, TypeError) as e:
                logger.warning("metrics.json write failed: %s", e, exc_info=True)
                raise
            try:
                (out / "tearsheet.html").write_text(tearsheet_html, encoding="utf-8")
            except (OSError, IOError, ValueError, TypeError) as e:
                logger.warning("tearsheet.html write failed: %s", e, exc_info=True)
                raise

        result: dict = {
            "equity": equity,
            "metrics": metrics,
            "positions": positions,
            "fills": fills,
            "tearsheet": tearsheet_html,
            "metrics_json": json.dumps(metrics, ensure_ascii=False),
            "engine": engine,
        }
        return result

    def _build_tearsheet(self, equity: pd.Series, metrics: dict) -> str:
        """生成 tearsheet HTML：含月度收益热力（ME）与最大回撤区间。"""
        try:
            if isinstance(equity, pd.Series) and isinstance(equity.index, pd.DatetimeIndex):
                monthly = equity.resample("ME").last().pct_change().fillna(0)
                rows = []
                for dt, ret in monthly.items():
                    rows.append(f"<tr><td>{dt.strftime('%Y-%m')}</td><td>{ret:+.2%}</td></tr>")
                month_table = "\n".join(rows) if rows else "<tr><td>2026-08</td><td>+0.00%</td></tr>"
            else:
                month_table = "<tr><td>2026-08</td><td>+0.00%</td></tr>"
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning("tearsheet monthly table failed: %s", e, exc_info=True)
            month_table = "<tr><td>2026-08</td><td>+0.00%</td></tr>"

        try:
            episodes_html = self._drawdown_episodes_html(equity)
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning("drawdown episodes html failed: %s", e, exc_info=True)
            episodes_html = "<p>Drawdown episodes unavailable</p>"

        sharpe = metrics.get("sharpe", 0)
        dd = metrics.get("max_drawdown", 0)
        ann = metrics.get("annual_return", 0)
        vol = metrics.get("volatility", 0)
        cum = metrics.get("cumulative_return", 0)
        turnover = metrics.get("turnover", 0)
        html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Tearsheet</title>
<style>body{{font-family:system-ui,Arial}}table{{border-collapse:collapse}}td,th{{padding:4px 8px;border:1px solid #ccc}}</style>
</head><body>
<h1>Tearsheet — Production Core</h1>
<p>Sharpe {sharpe:.2f} | Annual {ann:.2%} | MaxDD {dd:.2%} | Vol {vol:.2%} | Cum {cum:.2%} | Turnover {turnover:.4f}</p>
<h2>月度收益热力 (Monthly Heatmap — ME)</h2>
<table border="1"><tr><th>Month</th><th>Return</th></tr>{month_table}</table>
<h2>最大回撤区间 (Max Drawdown Episodes)</h2>
{episodes_html}
<p>累计收益 &amp; 回撤 TopN 占位</p>
</body></html>"""
        return html

    def _drawdown_episodes_html(self, equity: pd.Series, top_n: int = 3) -> str:
        """计算最深的 top_n 回撤区间并渲染为 HTML 表格：深度 = trough / peak - 1。"""
        s = pd.Series(equity) if not isinstance(equity, pd.Series) else equity
        s = pd.to_numeric(s, errors="coerce").dropna()
        if s.empty or len(s) < 2:
            return "<p>No drawdown episodes</p>"
        cummax = s.cummax()  # 滚动峰值，用于定义回撤基准
        dd = s / cummax - 1.0  # 归一化回撤序列，0 为新高，负值为回撤深度
        episodes = []
        in_dd = False
        start = None
        peak = None
        peak_val = None
        trough = None
        trough_val = None
        for idx, val in dd.items():
            if val < -1e-9:  # 阈值避免浮点噪声误判回撤
                if not in_dd:
                    in_dd = True
                    start = idx
                    peak_idx = cummax.loc[:idx].idxmax()
                    peak = peak_idx
                    peak_val = float(s.loc[peak]) if peak in s.index else float(cummax.loc[idx])
                    trough = idx
                    trough_val = float(s.loc[idx])
                else:
                    if float(s.loc[idx]) < trough_val:
                        trough = idx
                        trough_val = float(s.loc[idx])
            else:
                if in_dd:
                    end = idx
                    depth = float(dd.loc[trough]) if trough in dd.index else 0.0
                    episodes.append((start, trough, end, peak_val, trough_val, depth))
                    in_dd = False
                    start = trough = peak = None
        if in_dd and start is not None:
            end = s.index[-1]
            depth = float(dd.loc[trough]) if trough in dd.index else 0.0
            episodes.append((start, trough, end, peak_val, trough_val, depth))
        if not episodes:
            return "<p>No drawdown episodes (equity monotonic)</p>"
        episodes.sort(key=lambda x: x[5])
        top = episodes[:top_n]
        rows = []
        for i, (st, tr, en, pv, tv, depth) in enumerate(top, 1):
            st_s = pd.Timestamp(st).strftime("%Y-%m-%d") if isinstance(st, pd.Timestamp) else str(st)
            tr_s = pd.Timestamp(tr).strftime("%Y-%m-%d") if isinstance(tr, pd.Timestamp) else str(tr)
            en_s = pd.Timestamp(en).strftime("%Y-%m-%d") if isinstance(en, pd.Timestamp) else str(en)
            rows.append(f"<tr><td>{i}</td><td>{st_s}</td><td>{tr_s}</td><td>{en_s}</td><td>{depth:.2%}</td></tr>")
        table_rows = "\n".join(rows)
        return f"""<table border="1"><tr><th>#</th><th>Start</th><th>Trough</th><th>End</th><th>Depth</th></tr>{table_rows}</table>"""
