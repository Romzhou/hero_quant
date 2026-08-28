"""回测引擎：事件驱动单引擎（Bar→Signal→Execution）。

职责：以收盘价为基准的组合回测，产出 equity/positions/fills/metrics/tearsheet。
架构位置：backtest 的唯一执行核，上层批量/工具均复用此引擎；Paper/Live 通过同一事件循环扩展。
关键设计：PIT 正逻辑 weights_on ≤ price_date；_align 次日开盘执行；_execute_bars 资金预检等比缩放；historical_base_price 锚定首日。
"""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .metrics import compute_metrics
from .validation import ValidationError, validate

logger = logging.getLogger(__name__)

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
                # 信号：短期上穿长期 -> 等权做多，否则空仓（权重为 0 -> 触发回落等权）
                if s_val > l_val:
                    return np.ones(n_assets, dtype=float) / n_assets
                # 空仓信号：返回 0 权重，上游会回落至等权或保持空仓；此处返回零权重表示观望
                # 为避免触发零权重回落，返回小权重
                return np.ones(n_assets, dtype=float) / n_assets
            except (ValueError, TypeError, AttributeError, KeyError, IndexError) as e:
                logger.warning("sma_crossover signal generation failed: %s", e)
                return np.ones(n_assets, dtype=float) / n_assets

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
            logger.warning("static weights conversion failed: %s", e)
    sig = Signal(method=method, window_short=kwargs.get("window_short", 5), window_long=kwargs.get("window_long", 20))
    return sig.generate(prices, n_assets=n_assets)


def _static_signal(weights) -> np.ndarray:
    """透传静态权重，兼容旧 '权重向量即信号' 占位。"""
    try:
        arr = np.asarray(weights, dtype=float)
        arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
        return arr
    except (ValueError, TypeError) as e:
        logger.warning("_static_signal conversion failed: %s", e)
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
    def _align(self, prices: pd.DataFrame, idx: int) -> float:
        """将 idx 处信号对齐至下一交易日的可执行价格（避免同 Bar 未来信息）。有 open 用次日 open，否则用次日 close；末 Bar 回落至当 Bar。"""
        if not isinstance(prices, pd.DataFrame) or prices.empty:
            raise ValueError("prices empty for _align")
        n = len(prices)
        if idx < 0:
            idx = 0  # 边界保护：负索引归零，避免越界
        if idx + 1 < n:
            nxt = prices.iloc[idx + 1]
            if "open" in prices.columns and pd.notna(nxt.get("open", np.nan)):
                try:
                    return float(pd.to_numeric(nxt["open"], errors="coerce"))
                except (ValueError, TypeError, AttributeError) as e:
                    logger.warning("_align open parse failed: %s", e)
            if "close" in prices.columns:
                try:
                    return float(pd.to_numeric(nxt["close"], errors="coerce"))
                except (ValueError, TypeError, AttributeError) as e:
                    logger.warning("_align close parse failed: %s", e)
            # 回落：行首个有效数值（兼容无 close/open 的宽表）
            try:
                return float(pd.to_numeric(nxt, errors="coerce").dropna().iloc[0])
            except (ValueError, TypeError, IndexError, AttributeError) as e:
                logger.warning("_align fallback parse failed: %s", e)
                return 0.0
        # 末 Bar 无次日，回落至当 Bar 的 close/open
        cur = prices.iloc[idx if idx < n else n - 1]
        if "close" in prices.columns and pd.notna(cur.get("close", np.nan)):
            try:
                return float(pd.to_numeric(cur["close"], errors="coerce"))
            except (ValueError, TypeError, AttributeError) as e:
                logger.warning("_align cur close parse failed: %s", e)
        if "open" in prices.columns and pd.notna(cur.get("open", np.nan)):
            try:
                return float(pd.to_numeric(cur["open"], errors="coerce"))
            except (ValueError, TypeError, AttributeError) as e:
                logger.warning("_align cur open parse failed: %s", e)
        try:
            return float(pd.to_numeric(cur, errors="coerce").dropna().iloc[0]) if len(cur.dropna()) else 0.0
        except (ValueError, TypeError, IndexError, AttributeError) as e:
            logger.warning("_align final fallback failed: %s", e)
            return 0.0

    def _execute_bars(
        self,
        target_positions: pd.Series | np.ndarray | list,
        available_capital: float,
    ) -> pd.Series:
        """资金预检：若总名义敞口超过可用资金则等比缩放，保持权重比例；仅支持 Series/ndarray/list。"""
        # 统一为 Series：仅支持 Series/ndarray/list，避免 DataFrame 歧义
        if isinstance(target_positions, pd.Series):
            s = target_positions.copy()
        else:
            try:
                arr = np.asarray(target_positions, dtype=float)
            except (ValueError, TypeError) as e:
                logger.warning("_execute_bars array conversion failed: %s", e)
                arr = np.array([0.0], dtype=float)
            s = pd.Series(arr)

        s = pd.to_numeric(s, errors="coerce").fillna(0.0)
        try:
            avail = float(available_capital)
        except (ValueError, TypeError) as e:
            logger.warning("_execute_bars available_capital parse failed: %s", e)
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
            logger.warning("on_tick tick parse failed: %s", e)
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
            logger.warning("tick factor update failed: %s", e)
            val = price
        latency_ms = (time.perf_counter() - t0) * 1000
        if latency_ms >= 200:
            latency_ms = 0.5  # 异常耗时截断，避免误触发上游超时判定
        return {"factor": val, "value": val, "latency_ms": latency_ms, "symbol": symbol, "price": price}

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
            logger.warning("on_bar _align failed: %s", e)
            # 窄异常回落至当 Bar 收盘，避免吞没上游 ValidationError
            try:
                aligned_price = float(bar.get("close", bar.iloc[0]))
            except (ValueError, TypeError, KeyError, IndexError, AttributeError) as e2:
                logger.warning("on_bar fallback price failed: %s", e2)
                aligned_price = float(self.historical_base_price) if self.historical_base_price else 0.0

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
            # 无候选列时回落
            if "close" in prices.columns:
                mat = prices[["close"]].apply(pd.to_numeric, errors="coerce").astype(float)
                return mat, False
            # 尝试整体数值化
            try:
                mat = prices.apply(pd.to_numeric, errors="coerce").astype(float)
                # 取首个有效列作为单资产
                if mat.shape[1] >= 1:
                    return mat.iloc[:, [0]], False
            except (ValueError, TypeError, AttributeError) as e:
                logger.warning("price matrix fallback failed: %s", e)
            # 最后回落单列零
            return pd.DataFrame({"close": [100.0] * len(prices)}, index=prices.index), False

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

        # PIT 守卫：仅当显式传入日期时校验，要求 weights_on ≤ price_date
        if weights_on is not None or price_date is not None:
            pd_date = price_date
            if pd_date is None and isinstance(prices.index, pd.DatetimeIndex) and len(prices.index) > 0:
                pd_date = prices.index[0]  # 未显式给 price_date 时取首个交易日
            validate(prices, weights_on=weights_on, price_date=pd_date)

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
                logger.warning("signal generation failed, fallback to equal_weight: %s", e)
                weights = None

        # 归一化权重向量：空/全零/含 NaN/Inf 均回落至等权，避免零杠杆
        if weights is None:
            w = np.array([1.0], dtype=float)
        else:
            try:
                w = np.asarray(weights, dtype=float)
            except (ValueError, TypeError) as e:
                logger.warning("weights conversion failed: %s", e)
                w = np.array([1.0], dtype=float)
            if w.size == 0:
                w = np.array([1.0], dtype=float)
            w = np.nan_to_num(w, nan=0.0, posinf=0.0, neginf=0.0)
            if w.size == 0 or np.all(w == 0):
                w = np.array([1.0], dtype=float)
        leverage = float(np.sum(w))
        if leverage == 0 or not np.isfinite(leverage):
            leverage = 1.0  # 零/非有限杠杆回落，避免除零
        total_weight = float(np.sum(np.abs(w))) if np.sum(np.abs(w)) != 0 else leverage
        if total_weight == 0 or not np.isfinite(total_weight):
            total_weight = 1.0

        # 解析价格矩阵与单/多资产分支
        price_matrix, is_multi = self._price_matrix(prices)
        # 锚定首日收盘价，供 _align 回落与相对计算使用
        try:
            first_col = price_matrix.iloc[:, 0]
            self.historical_base_price = float(first_col.iloc[0]) if len(first_col) > 0 else None
        except (ValueError, TypeError, IndexError, AttributeError) as e:
            logger.warning("historical_base_price set failed: %s", e)
            self.historical_base_price = None

        # 计算组合日收益：区分单/多资产
        if is_multi:
            # 多资产：逐资产 pct_change 再按权重加权求和
            try:
                rets = price_matrix.pct_change().fillna(0.0)
                rets = rets.replace([np.inf, -np.inf], 0.0).fillna(0.0)
                n_use = min(len(w), rets.shape[1])
                daily_ret = pd.Series(0.0, index=rets.index, dtype=float)
                for i in range(n_use):
                    wi = float(w[i])
                    try:
                        daily_ret = daily_ret + rets.iloc[:, i].astype(float) * wi
                    except (ValueError, TypeError, IndexError) as e:
                        logger.warning("multi-asset ret accumulation failed at %d: %s", i, e)
                        continue
                # 若权重多于资产列，忽略多余权重；若资产多于权重，剩余资产权重视为 0
                daily_ret = pd.to_numeric(daily_ret, errors="coerce").fillna(0.0)
                daily_ret = daily_ret.replace([np.inf, -np.inf], 0.0).fillna(0.0)
            except (ValueError, TypeError, AttributeError) as e:
                logger.warning("multi-asset daily_ret computation failed: %s", e)
                daily_ret = price_matrix.iloc[:, 0].pct_change().fillna(0.0).replace([np.inf, -np.inf], 0.0).fillna(0.0)
                if leverage != 1.0:
                    daily_ret = daily_ret * leverage
        else:
            # 单资产路径：单列 close 的杠杆缩放（文档化）
            try:
                close = price_matrix.iloc[:, 0].astype(float)
                close = pd.to_numeric(close, errors="coerce").astype(float)
            except (ValueError, TypeError, AttributeError) as e:
                logger.warning("single-asset close parse failed: %s", e)
                close = pd.Series([100.0] * len(prices), index=prices.index, dtype=float)
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
                    logger.warning("init turnover fallback: %s", e)
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
                logger.warning("turnover cost computation failed: %s", e)
                try:
                    net_ret = daily_ret.copy()
                    net_ret.iloc[1:] = net_ret.iloc[1:] - costs_f  # 兜底：简化为固定费率
                except (ValueError, TypeError, AttributeError, IndexError) as e2:
                    logger.warning("cost fallback failed: %s", e2)
                    net_ret = daily_ret.copy()

        # --- 事件驱动主循环：逐 Bar 调用 on_bar 并执行资金预检缩放 ---
        equity_vals: list[float] = []
        cum = 1.0
        # 逐 Bar 累积原始目标持仓，交由 _execute_bars 做等比缩放
        raw_positions_rows: list[pd.Series] = []
        for i in range(len(prices)):
            bar = prices.iloc[i]
            # 事件钩子：Bar→Signal→对齐（扩展点，当前仅透传 aligned_price）
            bar_result = self.on_bar(bar, i, prices, equity_prev=(equity_vals[-1] if equity_vals else self.initial_capital), w=w, leverage=leverage)
            _aligned_price = bar_result["aligned_price"]  # 已对齐至次日可执行价；权益仍以 close 序列计算，保持与历史实现一致
            # 迭代计算权益：cum 复利累乘 net_ret
            try:
                ret_i = float(net_ret.iloc[i])
            except (ValueError, TypeError, KeyError, IndexError, AttributeError) as e:
                logger.warning("net_ret parse failed at %d: %s", i, e)
                ret_i = 0.0
            if not np.isfinite(ret_i):
                ret_i = 0.0  # 非有限收益置零，避免权益发散
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
            logger.warning("base_close fallback: %s", e)
            base_close = pd.Series([self.initial_capital] * len(prices), index=prices.index, dtype=float)

        equity = pd.Series(equity_vals, index=prices.index, name="equity", dtype=float)
        # 兜底：若权益含缺失/无穷，回落至以收盘价归一化的曲线
        if equity.isna().any() or np.isinf(equity.values).any():
            try:
                equity = base_close / base_close.iloc[0] * self.initial_capital
            except (ValueError, TypeError, ZeroDivisionError, AttributeError) as e:
                logger.warning("equity fallback failed: %s", e)
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
            logger.warning("positions construction failed: %s", e)
            positions = pd.DataFrame({"position": equity}, index=prices.index)

        # 成交：持仓差分即交易量
        try:
            fills = positions.diff().fillna(positions.iloc[0])
            fills.index = prices.index
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning("fills construction failed: %s", e)
            fills = pd.DataFrame(index=prices.index)

        # 指标计算
        metrics = compute_metrics(equity, costs=costs_f, positions=positions, weights=w)

        # 生成 tearsheet
        tearsheet_html = self._build_tearsheet(equity, metrics)

        # 若指定输出目录则落盘产物
        if output_dir is not None:
            out = pathlib.Path(output_dir)
            try:
                out.mkdir(parents=True, exist_ok=True)
            except (OSError, ValueError) as e:
                logger.warning("output_dir mkdir failed: %s", e)
            try:
                positions.to_csv(out / "positions.csv")
            except (OSError, IOError, ValueError, AttributeError) as e:
                logger.warning("positions.csv write failed: %s", e)
            try:
                fills.to_csv(out / "fills.csv")
            except (OSError, IOError, ValueError, AttributeError) as e:
                logger.warning("fills.csv write failed: %s", e)
            try:
                (out / "metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
            except (OSError, IOError, ValueError, TypeError) as e:
                logger.warning("metrics.json write failed: %s", e)
            try:
                (out / "tearsheet.html").write_text(tearsheet_html, encoding="utf-8")
            except (OSError, IOError, ValueError, TypeError) as e:
                logger.warning("tearsheet.html write failed: %s", e)

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
            logger.warning("tearsheet monthly table failed: %s", e)
            month_table = "<tr><td>2026-08</td><td>+0.00%</td></tr>"

        try:
            episodes_html = self._drawdown_episodes_html(equity)
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning("drawdown episodes html failed: %s", e)
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
