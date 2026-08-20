"""BacktestEngine — event-driven single engine (Bar→Signal→Execution, PIT, proportional scaling).

P0 Foundation Parity Task 5: refactor run -> on_bar loop + historical_base_price + _align next-day open + _execute_bars capital pre-check.
Keeps metrics/tearsheet intact; three-state (Backtest→Paper→Live) will reuse same engine via Temporal.
"""

from __future__ import annotations

import json
import pathlib
import numpy as np
import pandas as pd

from .metrics import compute_metrics
from .validation import validate, ValidationError


class BacktestEngine:
    """Event-driven single engine (production-core).

    Provides `run(prices, weights, costs=0.0005, output_dir=None, engine="default") -> dict`

    Event loop: Bar -> Signal (weights * leverage) -> _align (next-day open) -> _execute_bars (capital pre-check)
    Invariants: PIT weights_on <= price_date, historical_base_price anchored, mixed currency rejected (via validate).
    """

    def __init__(self, initial_capital: float = 1.0):
        cap = float(initial_capital)
        if not np.isfinite(cap) or cap <= 0:
            raise ValueError(f"initial_capital must be >0 and finite, got {initial_capital!r}")
        self.initial_capital = cap
        self.historical_base_price: float | None = None

    # ------------------------------------------------------------------ helpers
    def _align(self, prices: pd.DataFrame, idx: int) -> float:
        """Align signal at bar idx to next-day open price (execution).

        If 'open' column exists, use next bar's open; otherwise next bar's close.
        Falls back to current bar's close/open at end.
        """
        if not isinstance(prices, pd.DataFrame) or prices.empty:
            raise ValueError("prices empty for _align")
        n = len(prices)
        if idx < 0:
            idx = 0
        if idx + 1 < n:
            nxt = prices.iloc[idx + 1]
            if "open" in prices.columns and pd.notna(nxt.get("open", np.nan)):
                try:
                    return float(pd.to_numeric(nxt["open"], errors="coerce"))
                except Exception:
                    pass
            if "close" in prices.columns:
                return float(pd.to_numeric(nxt["close"], errors="coerce"))
            # fallback: first numeric in row
            return float(pd.to_numeric(nxt, errors="coerce").dropna().iloc[0])
        # last bar: no next day, use current
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
        """Capital pre-check proportional scaling (Series | ndarray | list only).

        If sum|target| > available_capital, scale down proportionally to preserve weight ratios.
        DataFrame input is intentionally unsupported (YAGNI) — use Series per-bar.
        """
        # Normalize to Series (Series | ndarray | list only)
        if isinstance(target_positions, pd.Series):
            s = target_positions.copy()
        else:
            arr = np.asarray(target_positions, dtype=float)
            s = pd.Series(arr)

        s = pd.to_numeric(s, errors="coerce").fillna(0.0)
        try:
            avail = float(available_capital)
        except Exception:
            avail = self.initial_capital
        if not np.isfinite(avail) or avail <= 0:
            avail = self.initial_capital if self.initial_capital > 0 else 1.0

        total = float(s.abs().sum())
        if total > avail and total > 0:
            factor = avail / total
            s = s * factor
        # ensure no inf
        s = s.replace([np.inf, -np.inf], 0.0).fillna(0.0)
        return s

    def on_bar(
        self,
        bar: pd.Series,
        idx: int,
        prices: pd.DataFrame,
        equity_prev: float | None = None,
        w: np.ndarray | None = None,
        leverage: float | None = None,
    ) -> dict:
        """Process single bar (event-driven) — ExtensionPoint.

        ExtensionPoint: on_bar is intentionally not wired to equity calculation in
        the current vectorized run loop. It returns ``aligned_price`` (next-day
        open via _align) for PIT-safe execution, but ``run`` computes equity from
        ``close`` pct_change and does NOT use aligned_price for pricing. This is
        explicit to avoid dead-hook confusion. Wiring aligned_price into execution
        is deferred to Task17 (streaming on_tick).

        Returns:
            dict with 'bar', 'aligned_price' (next-day open), 'idx', 'equity_prev'
        """
        # PIT / validation is handled at run level; on_bar is pure per-bar logic
        try:
            aligned_price = self._align(prices, idx)
        except ValidationError:
            raise
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            # fallback to bar close — narrow, not broad Exception
            try:
                aligned_price = float(bar.get("close", bar.iloc[0]))
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                aligned_price = float(self.historical_base_price) if self.historical_base_price else 0.0

        # Signal placeholder: weight vector is signal; execution will be handled by _execute_bars in run loop
        # Keep hook for future Signal→Execution branching (limit_band, etc.)
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
        # M3 guard: initial_capital must be >0 and finite at run entry
        if not np.isfinite(self.initial_capital) or self.initial_capital <= 0:
            raise ValueError(f"initial_capital must be >0 and finite, got {self.initial_capital!r}")
        # --- Input validation: empty / invalid prices ---
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

        # Optional PIT guard — if caller provides weights_on/price_date, validate 正逻辑
        if weights_on is not None or price_date is not None:
            pd_date = price_date
            if pd_date is None and isinstance(prices.index, pd.DatetimeIndex) and len(prices.index) > 0:
                pd_date = prices.index[0]
            validate(prices, weights_on=weights_on, price_date=pd_date)

        # Normalize weights -> vector
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
            leverage = 1.0

        close = pd.to_numeric(prices["close"], errors="coerce").astype(float)
        # historical_base_price: anchor first close for PIT-safe relative calcs
        try:
            self.historical_base_price = float(close.iloc[0]) if len(close) > 0 else None
        except Exception:
            self.historical_base_price = None

        # daily returns scaled by leverage (synthetic multi-asset proxy)
        daily_ret = close.pct_change().fillna(0.0)
        daily_ret = daily_ret.replace([np.inf, -np.inf], 0.0).fillna(0.0)
        if leverage != 1.0:
            daily_ret = daily_ret * leverage

        # --- Costs applied per turnover (same as before, kept vector for parity) ---
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
                equity_safe = gross_equity.replace(0, np.nan).fillna(self.initial_capital)
                turnover_rate = turnover_series / equity_safe
                turnover_rate = turnover_rate.replace([np.inf, -np.inf], 0.0).fillna(0.0)
                if len(turnover_rate) > 0:
                    turnover_rate.iloc[0] = 1.0 if turnover_rate.iloc[0] == 0 else turnover_rate.iloc[0]
                cost_drag = turnover_rate * costs_f
                net_ret = daily_ret - cost_drag
                net_ret = net_ret.replace([np.inf, -np.inf], 0.0).fillna(0.0)
            except Exception:
                net_ret = daily_ret.copy()
                net_ret.iloc[1:] = net_ret.iloc[1:] - costs_f

        # --- Event-driven single engine: on_bar loop + historical_base_price + _execute_bars ---
        # Iterate bars sequentially, calling on_bar and applying capital pre-check scaling to positions.
        equity_vals: list[float] = []
        cum = 1.0
        # Precompute positions raw per bar via event loop, then scale via _execute_bars
        raw_positions_rows: list[pd.Series] = []
        for i in range(len(prices)):
            bar = prices.iloc[i]
            # event hook: Bar -> Signal -> Execution alignment (ExtensionPoint)
            bar_result = self.on_bar(bar, i, prices, equity_prev=(equity_vals[-1] if equity_vals else self.initial_capital), w=w, leverage=leverage)
            _aligned_price = bar_result["aligned_price"]  # returned but not used for equity; TODO(Task17) wire into execution when streaming on_tick
            # compute equity iteratively from net_ret
            try:
                ret_i = float(net_ret.iloc[i])
            except Exception:
                ret_i = 0.0
            if not np.isfinite(ret_i):
                ret_i = 0.0
            cum = cum * (1 + ret_i)
            eq = cum * self.initial_capital
            if not np.isfinite(eq):
                eq = self.initial_capital
            equity_vals.append(float(eq))

            # Build raw target positions for this bar
            n_assets = len(w)
            if n_assets > 1:
                raw = {f"asset_{i}": eq * float(wi) / leverage for i, wi in enumerate(w)}
            else:
                raw = {"position": eq * leverage}
            raw_s = pd.Series(raw, dtype=float)
            # capital pre-check proportional scaling (ensure gross <= equity)
            scaled = self._execute_bars(raw_s, available_capital=eq)
            raw_positions_rows.append(scaled)

        equity = pd.Series(equity_vals, index=prices.index, name="equity", dtype=float)
        # Fallback if equity has inf/na
        if equity.isna().any() or np.isinf(equity.values).any():
            equity = close / close.iloc[0] * self.initial_capital
            equity.name = "equity"
            equity.index = prices.index
        equity = pd.to_numeric(equity, errors="coerce").fillna(self.initial_capital)
        equity = equity.replace([np.inf, -np.inf], self.initial_capital)
        equity.name = "equity"
        equity.index = prices.index

        # Positions from event loop (already scaled)
        try:
            if raw_positions_rows and len(raw_positions_rows) == len(prices):
                positions = pd.DataFrame(raw_positions_rows, index=prices.index)
                # Ensure columns consistent: for single asset, column is "position"
                if positions.shape[1] == 0:
                    positions = pd.DataFrame({"position": equity * leverage}, index=prices.index)
            else:
                # fallback vector path (should not happen)
                n_assets = len(w)
                if n_assets > 1:
                    pos_dict = {f"asset_{i}": equity * float(wi) / leverage for i, wi in enumerate(w)}
                    positions = pd.DataFrame(pos_dict, index=prices.index)
                else:
                    positions = pd.DataFrame({"position": equity * leverage}, index=prices.index)
        except Exception:
            positions = pd.DataFrame({"position": equity}, index=prices.index)

        # Fills — position diff as trades (fills = positions.diff())
        try:
            fills = positions.diff().fillna(positions.iloc[0])
            fills.index = prices.index
        except Exception:
            fills = pd.DataFrame(index=prices.index)

        # Metrics via compute_metrics
        metrics = compute_metrics(equity, costs=costs_f, positions=positions, weights=w)

        # Tearsheet
        tearsheet_html = self._build_tearsheet(equity, metrics)

        # Materialize artifacts if output_dir given
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
        """Build tearsheet html with monthly heatmap (ME) and max drawdown episodes."""
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
        """Compute top drawdown episodes and render as HTML table."""
        s = pd.Series(equity) if not isinstance(equity, pd.Series) else equity
        s = pd.to_numeric(s, errors="coerce").dropna()
        if s.empty or len(s) < 2:
            return "<p>No drawdown episodes</p>"
        cummax = s.cummax()
        dd = s / cummax - 1.0
        episodes = []
        in_dd = False
        start = None
        peak = None
        peak_val = None
        trough = None
        trough_val = None
        for idx, val in dd.items():
            if val < -1e-9:
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
