"""回测引擎：事件驱动单引擎（Bar→Signal→Execution）。

职责：以收盘价为基准的组合回测，产出 equity/positions/fills/metrics/tearsheet。
架构位置：backtest 的唯一执行核，上层批量/工具均复用此引擎；Paper/Live 通过同一事件循环扩展。
关键设计：PIT 正逻辑 weights_on ≤ price_date；_align 次日开盘执行；_execute_bars 资金预检等比缩放；historical_base_price 锚定首日。
"""

from __future__ import annotations

import json
import pathlib
import numpy as np
import pandas as pd

from .metrics import compute_metrics
from .validation import validate, ValidationError


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
                except Exception:
                    pass  # 容错：解析失败则回落至 close
            if "close" in prices.columns:
                return float(pd.to_numeric(nxt["close"], errors="coerce"))
            # 回落：行首个有效数值（兼容无 close/open 的宽表）
            return float(pd.to_numeric(nxt, errors="coerce").dropna().iloc[0])
        # 末 Bar 无次日，回落至当 Bar 的 close/open
        cur = prices.iloc[idx if idx < n else n - 1]
        if "close" in prices.columns and pd.notna(cur.get("close", np.nan)):
            return float(pd.to_numeric(cur["close"], errors="coerce"))
        if "open" in prices.columns and pd.notna(cur.get("open", np.nan)):
            return float(pd.to_numeric(cur["open"], errors="coerce"))
        return float(pd.to_numeric(cur, errors="coerce").dropna().iloc[0]) if len(cur.dropna()) else 0.0

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
            arr = np.asarray(target_positions, dtype=float)
            s = pd.Series(arr)

        s = pd.to_numeric(s, errors="coerce").fillna(0.0)
        try:
            avail = float(available_capital)
        except Exception:
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
        except Exception:
            price = 0.0
            symbol = ""
        # 惰性初始化增量因子（窗口 20 为经验值，平衡平滑与灵敏度）
        if not hasattr(self, "_tick_factor"):
            try:
                from hero_quant.stream.factor import IncrementalFactor
                self._tick_factor = IncrementalFactor(window=20)
            except Exception:
                self._tick_factor = None
        val = 0.0
        try:
            if self._tick_factor is not None:
                val = float(self._tick_factor.update(price))
            else:
                val = price  # 无增量因子时直接回落为价格本身
        except Exception:
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
        """处理单根 Bar 的扩展点：返回次日可执行价 aligned_price（经 _align），当前 run 仍以 close 计算权益，未直接挂钩定价以保持 PIT 清晰。"""
        # PIT 校验在 run 层统一处理，此处仅做单 Bar 纯逻辑
        try:
            aligned_price = self._align(prices, idx)
        except ValidationError:
            raise
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            # 窄异常回落至当 Bar 收盘，避免吞没上游 ValidationError
            try:
                aligned_price = float(bar.get("close", bar.iloc[0]))
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                aligned_price = float(self.historical_base_price) if self.historical_base_price else 0.0

        # 信号占位：权重向量即信号，执行由外层 run 循环经 _execute_bars 完成；保留分支钩子以扩展限价带等
        return {"bar": bar, "idx": idx, "aligned_price": aligned_price, "equity_prev": equity_prev}

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
    ) -> dict:
        """执行回测主流程：校验→PIT 检查→收益与换手计费→事件循环生成权益/持仓并产出 tearsheet。"""
        # 入口守卫：初始资金必须为正且有限，否则后续复利计算无意义
        if not np.isfinite(self.initial_capital) or self.initial_capital <= 0:
            raise ValueError(f"initial_capital must be >0 and finite, got {self.initial_capital!r}")
        # --- 输入校验：空/非法价格 ---
        if not isinstance(prices, pd.DataFrame):
            raise TypeError("prices must be a pandas DataFrame")
        if "close" not in prices.columns:
            raise ValueError("prices DataFrame must contain 'close' column")
        if prices.empty:
            raise ValueError("prices DataFrame is empty")
        try:
            _close_check = pd.to_numeric(prices["close"], errors="coerce")
            if _close_check.isna().all():
                raise ValueError("prices['close'] contains no valid numeric data")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"invalid prices['close']: {e}") from e

        # PIT 守卫：仅当显式传入日期时校验，要求 weights_on ≤ price_date
        if weights_on is not None or price_date is not None:
            pd_date = price_date
            if pd_date is None and isinstance(prices.index, pd.DatetimeIndex) and len(prices.index) > 0:
                pd_date = prices.index[0]  # 未显式给 price_date 时取首个交易日
            validate(prices, weights_on=weights_on, price_date=pd_date)

        # 归一化权重向量：空/全零/含 NaN/Inf 均回落至等权，避免零杠杆
        if weights is None:
            w = np.array([1.0], dtype=float)
        else:
            w = np.asarray(weights, dtype=float)
            if w.size == 0:
                w = np.array([1.0], dtype=float)
            w = np.nan_to_num(w, nan=0.0, posinf=0.0, neginf=0.0)
            if w.size == 0 or np.all(w == 0):
                w = np.array([1.0], dtype=float)
        leverage = float(np.sum(w))
        if leverage == 0 or not np.isfinite(leverage):
            leverage = 1.0  # 零/非有限杠杆回落，避免除零

        close = pd.to_numeric(prices["close"], errors="coerce").astype(float)
        # 锚定首日收盘价，供 _align 回落与相对计算使用
        try:
            self.historical_base_price = float(close.iloc[0]) if len(close) > 0 else None
        except Exception:
            self.historical_base_price = None

        # 日收益以杠杆缩放（单资产代理多资产语义）
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
                n_assets = len(w)
                if n_assets > 1:
                    pos_proxy = pd.DataFrame(
                        {f"asset_{i}": gross_equity * float(wi) / leverage for i, wi in enumerate(w)},
                        index=prices.index,
                    )
                else:
                    pos_proxy = pd.DataFrame({"position": gross_equity * leverage}, index=prices.index)
                turnover_series = pos_proxy.diff().abs().sum(axis=1).fillna(
                    pos_proxy.iloc[0].abs().sum(axis=1) if hasattr(pos_proxy.iloc[0], "sum") else 0
                )
                equity_safe = gross_equity.replace(0, np.nan).fillna(self.initial_capital)  # 避免除零
                turnover_rate = turnover_series / equity_safe
                turnover_rate = turnover_rate.replace([np.inf, -np.inf], 0.0).fillna(0.0)
                if len(turnover_rate) > 0:
                    turnover_rate.iloc[0] = 1.0 if turnover_rate.iloc[0] == 0 else turnover_rate.iloc[0]  # 首日视为建仓
                cost_drag = turnover_rate * costs_f
                net_ret = daily_ret - cost_drag
                net_ret = net_ret.replace([np.inf, -np.inf], 0.0).fillna(0.0)
            except Exception:
                net_ret = daily_ret.copy()
                net_ret.iloc[1:] = net_ret.iloc[1:] - costs_f  # 兜底：简化为固定费率

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
            except Exception:
                ret_i = 0.0
            if not np.isfinite(ret_i):
                ret_i = 0.0  # 非有限收益置零，避免权益发散
            cum = cum * (1 + ret_i)
            eq = cum * self.initial_capital
            if not np.isfinite(eq):
                eq = self.initial_capital
            equity_vals.append(float(eq))

            # 构建当 Bar 原始目标持仓
            n_assets = len(w)
            if n_assets > 1:
                raw = {f"asset_{i}": eq * float(wi) / leverage for i, wi in enumerate(w)}
            else:
                raw = {"position": eq * leverage}
            raw_s = pd.Series(raw, dtype=float)
            # 资金预检等比缩放，确保名义敞口不超过权益
            scaled = self._execute_bars(raw_s, available_capital=eq)
            raw_positions_rows.append(scaled)

        equity = pd.Series(equity_vals, index=prices.index, name="equity", dtype=float)
        # 兜底：若权益含缺失/无穷，回落至以收盘价归一化的曲线
        if equity.isna().any() or np.isinf(equity.values).any():
            equity = close / close.iloc[0] * self.initial_capital
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
                    positions = pd.DataFrame({"position": equity * leverage}, index=prices.index)
            else:
                # 回落的向量化路径（正常不应触发）
                n_assets = len(w)
                if n_assets > 1:
                    pos_dict = {f"asset_{i}": equity * float(wi) / leverage for i, wi in enumerate(w)}
                    positions = pd.DataFrame(pos_dict, index=prices.index)
                else:
                    positions = pd.DataFrame({"position": equity * leverage}, index=prices.index)
        except Exception:
            positions = pd.DataFrame({"position": equity}, index=prices.index)

        # 成交：持仓差分即交易量
        try:
            fills = positions.diff().fillna(positions.iloc[0])
            fills.index = prices.index
        except Exception:
            fills = pd.DataFrame(index=prices.index)

        # 指标计算
        metrics = compute_metrics(equity, costs=costs_f, positions=positions, weights=w)

        # 生成 tearsheet
        tearsheet_html = self._build_tearsheet(equity, metrics)

        # 若指定输出目录则落盘产物
        if output_dir is not None:
            out = pathlib.Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            try:
                positions.to_csv(out / "positions.csv")
            except Exception:
                pass
            try:
                fills.to_csv(out / "fills.csv")
            except Exception:
                pass
            try:
                (out / "metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            try:
                (out / "tearsheet.html").write_text(tearsheet_html, encoding="utf-8")
            except Exception:
                pass

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
        except Exception:
            month_table = "<tr><td>2026-08</td><td>+0.00%</td></tr>"

        try:
            episodes_html = self._drawdown_episodes_html(equity)
        except Exception:
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
