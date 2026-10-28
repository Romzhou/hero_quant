"""回测工具集：PIT 正确性校验、引擎执行与指标计算。

位于 tools 层回测分支，封装 BacktestEngine 的执行与校验逻辑；
run_backtest/optimize_portfolio 涉及状态写，并发安全标 False，其余只读工具标 True。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict

from hero_quant.tools.registry import tool

logger = logging.getLogger(__name__)

try:
    import structlog as _structlog  # type: ignore

    _slog = _structlog.get_logger(__name__)
except Exception:  # pragma: no cover — 最小环境无 structlog
    _slog = None  # type: ignore

_CHAIN = "B"  # F线分级：本文件位于回测链（工具侧行情拉取/合成构造）


def _warn(op: str, msg: str, *args: Any, **fields: Any) -> None:
    """F线分级告警（D线 risk 同款 schema）：stdlib warning 为主，structlog 结构化为辅
    （error_code + chain；trace_id 由 server.py contextvars 自动透传）。PII 约束：只传
    非敏感字段，禁标的原文外价格序列。"""
    _exc_info = fields.pop("exc_info", None)
    try:
        logger.warning(msg, *args, exc_info=_exc_info)
    except Exception:
        pass
    if _slog is None:
        return
    try:
        _slog.warning(op, error_code=op, chain=fields.pop("chain", _CHAIN), exc_info=_exc_info, **fields)
    except Exception:
        pass


def _fetch_bars_for_backtest(symbol: str, start: str, end: str, interval: str = "1d"):
    """为回测拉取行情，返回 (bars, provenance_dict|None, error|None)。

    中文：此前丢弃 provenance（bars, _），导致 run_backtest 无法区分真实/合成、
    无法透传 unit/adjust。修复后返回三元组；第三项是拉取失败的原始原因，
    调用方据此 fail-closed（断网不得用合成 bars 冒充行情）。
    兼容旧桩：仍接受纯 bars 或二元组。
    """
    try:
        from hero_quant.data.registry import MarketDataRegistry
        from hero_quant.data.loaders.tencent import TencentLoader

        reg = MarketDataRegistry()
        reg.register(TencentLoader())
        try:
            from hero_quant.data.loaders.yahoo import YahooLoader

            reg.register(YahooLoader())
        except (ImportError, ModuleNotFoundError, AttributeError, ValueError) as e:
            _warn("yahoo_loader_register_failed", "YahooLoader register failed error_code=yahoo_loader_register_failed: %s", e, exc_info=True)
        # Use keyword interval for clarity; positional shim is brittle
        bars, prov = reg.get_bars(symbol, start, end, interval=interval)
        prov_dict = None
        try:
            if prov is not None:
                source = getattr(prov, "source", None)
                unit = getattr(prov, "unit", None)
                if isinstance(source, str) and source.strip() and unit in ("board_lots", "shares"):
                    prov_dict = {"source": source.strip(), "unit": unit}
                    _adj = getattr(prov, "adjust", None)
                    if _adj:
                        prov_dict["adjust"] = _adj
                    _asof = getattr(prov, "factor_asof", None)
                    if _asof is not None:
                        prov_dict["factor_asof"] = _asof
                    _extra = getattr(prov, "extra", None)
                    if _extra:
                        prov_dict["extra"] = _extra
        except (AttributeError, TypeError, ValueError) as e:
            logger.warning("fetch provenance export failed for %s: %s", symbol, e, exc_info=True)
            prov_dict = None
        return bars, prov_dict, None
    except (ValueError, TypeError, AttributeError, ImportError, RuntimeError, OSError) as e:
        logger.warning("fetch bars failed for %s: %s", symbol, e, exc_info=True)
        # 获取失败必须把原因透出：断网/接口报错不得静默变成“无数据”，
        # 更不得被合成 bars 顶替（run_backtest 据此返回 ok:False）。
        return [], None, f"{type(e).__name__}: {e}"


def _synthetic_prices_for_backtest(index, ticker: str):
    """按 ticker 生成确定性合成价格（趋势+噪声），用于多资产回测演示，复用 bench 逻辑。"""
    import numpy as np
    import pandas as pd

    n = len(index)
    if n == 0:
        df = pd.DataFrame({"close": pd.Series(dtype=float)}, index=index)
        df["open"] = pd.Series(dtype=float)
        return df
    # stable hash via sha256
    seed = int.from_bytes(hashlib.sha256(str(ticker).encode()).digest()[:4], "big")
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 0.5, size=n)
    trend = np.arange(n) * 0.3
    close = 100 + trend + np.cumsum(noise) * 0.2
    close = np.maximum(close, 1.0)
    df = pd.DataFrame({"close": close.astype(float)}, index=index)
    try:
        df["open"] = df["close"].shift(1).fillna(df["close"].iloc[0])
    except (ValueError, TypeError, AttributeError, IndexError, KeyError) as e:
        # intentional: 合成帧构造探针（失败即 open=close）；确定性合成路径，属构造期 garnish
        logger.debug("synthetic open fill failed: %s error_code=synthetic_open_fill_failed", e, exc_info=True)
        df["open"] = df["close"]
    return df


@tool(
    name="run_backtest",
    description="Run PIT-correct backtest for a symbol/weights over date range (engine-backed, costs, engine param).",
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "weights": {"type": "array"},
            "costs": {"type": "number"},
            "engine": {"type": "string"},
            "interval": {"type": "string"},
            "allow_synthetic": {"type": "boolean"},
            "as_of": {"type": "string"},
            "weights_on": {"type": "string"},
        },
        "required": ["symbol"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {
            "equity": {"type": "array"},
            "metrics": {"type": "object"},
            "ok": {"type": "boolean"},
            "error": {"type": "string"},
            "engine": {"type": "string"},
            "provenance": {"type": "object"},
            "non_pit": {"type": "boolean"},
            "pit_basis": {"type": "string"},
        },
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: False,
)
def run_backtest(
    symbol: str = "600519.SH",
    start: str = "2026-08-01",
    end: str = "2026-08-03",
    weights: list | None = None,
    costs: float = 0.0005,
    engine: str = "default",
    interval: str = "1d",
    allow_synthetic: bool = True,
    as_of: str | None = None,
    weights_on: str | None = None,
) -> Dict[str, Any]:
    """执行 PIT 正确回测，含交易成本与多引擎支持；合成兜底需显式 allow_synthetic=True。

    PIT 基准日（2026-10-28 修复）：
    - 显式 as_of 优先；若晚于最后一根 K 线直接 {ok:False}（未来数据 fail-closed）。
    - as_of 缺省时取 prices.index[-1] 作为 price_date 传引擎（此前恒不传，真实行情
      100% 撞 engine.py 的 PIT fail-closed，equity 恒空）。
    - 推断出的基准日不是调用方声明的 PIT 时点，返回体必须 non_pit=True 且
      pit_basis="inferred_as_of"；显式 as_of 记 "explicit_as_of"。
    - weights_on 缺省交给引擎（= price_date）；显式传入时引擎校验 weights_on<=price_date。

    中文：此前 is_synthetic 自动 opt-in allow_synthetic 旁路 PIT，且 provenance
    丢 unit 字段。修复后：无真实行情默认 fail-closed 返回 {ok:False}（不再静默
    合成）；仅当显式 allow_synthetic=True 才走合成演示路径（仍带 provenance
    synthetic 标记 + 自动 opt-in 引擎 allow_synthetic）。

    多资产支持：
    - 若 symbol 包含逗号（如 "AAPL,MSFT"），按逗号分割为多标的，为每个标的合成独立价格序列，构造多列 DataFrame 传入引擎以触发多资产路径。
    - 若 weights 长度 >1 但仅有单列价格，则生成多列合成价格作为 fallback，并在日志中警告。
    - 单标的场景保持原有单列 'close' DataFrame 行为以兼容存量调用。
    - 无真实行情时仍可运行，但返回体必带 provenance{source:"synthetic"} 标记，
      调用方不得将其误判为真实市场回测。
    """
    # 中文：默认权重按 symbol 形态决定——单标的默认 [1.0] 走单资产路径，
    # 逗号多标的默认等权；禁止单标的被默认 [0.5,0.5] 逼入多资产合成路径
    if weights is None:
        if isinstance(symbol, str) and "," in symbol:
            tickers_default = [s.strip() for s in symbol.split(",") if s.strip()]
            n_default = len(tickers_default) or 1
            weights = [1.0 / n_default] * n_default
        else:
            weights = [1.0]

    def _prov_envelope(src: str, live_prov: dict | None = None) -> dict:
        """provenance 信封：必带 unit（冻结 schema），并透传 adjust/factor_asof/extra。

        中文：此前丢 unit 字段，下游 grounding 按 schema 拒收或误读手/股。
        synthetic → unit shares + adjust none；真实路径沿用 loader provenance；
        桩/缺失时按 symbol 后缀推断（.SH/.SZ→board_lots，其余 shares）。
        """
        if src == "synthetic":
            # 中文：合成数据的 unit 仍须跟随标的市场口径，A 股合成序列不是 shares
            # （board_lots vs shares 差 100 倍，统一写 shares 会把 unit 弄错）。
            _sup = str(symbol or "").upper()
            _sunit = "board_lots" if (_sup.endswith(".SH") or _sup.endswith(".SZ")) else "shares"
            return {"source": "synthetic", "unit": _sunit, "adjust": "none"}
        if isinstance(live_prov, dict) and live_prov.get("source") and live_prov.get("unit") in ("board_lots", "shares"):
            out = {"source": live_prov["source"], "unit": live_prov["unit"]}
            if live_prov.get("adjust"):
                out["adjust"] = live_prov["adjust"]
            if live_prov.get("factor_asof") is not None:
                out["factor_asof"] = live_prov["factor_asof"]
            if live_prov.get("extra"):
                out["extra"] = live_prov["extra"]
            return out
        _up = str(symbol or "").upper()
        _unit = "board_lots" if (_up.endswith(".SH") or _up.endswith(".SZ")) else "shares"
        return {"source": "market", "unit": _unit, "adjust": "unknown"}

    # 逗号多标的必查（即使 weights 长度为 1，也属 tickers-vs-weights 错位）。
    # 提前到行情拉取之前：参数错位是调用方错误，不应被“拉取失败”信封掩盖。
    is_comma_symbol = isinstance(symbol, str) and "," in symbol
    tickers = [s.strip() for s in symbol.split(",") if s.strip()] if is_comma_symbol else []
    if is_comma_symbol and len(tickers) != len(weights):
        logger.warning("tickers %d vs weights %d mismatch for %s", len(tickers), len(weights), symbol)
        return {
            "equity": [],
            "metrics": {},
            "ok": False,
            "error": f"tickers {len(tickers)} vs weights {len(weights)} mismatch",
            "engine": engine or "default",
            "provenance": _prov_envelope("market", None),
        }

    # Narrow try blocks: date_range isolated
    import pandas as pd

    _fetch_res = _fetch_bars_for_backtest(symbol, start, end, interval=interval)
    # 兼容旧桩：_fetch_bars_for_backtest 可能被单测桩为纯 bars list / 二元组
    fetch_error = None
    if isinstance(_fetch_res, tuple) and len(_fetch_res) == 3:
        bars, live_prov, fetch_error = _fetch_res
    elif isinstance(_fetch_res, tuple) and len(_fetch_res) == 2:
        bars, live_prov = _fetch_res
    else:
        bars, live_prov = _fetch_res, None
    # 中文：不得静默截断——全部 bars 进入引擎，避免长区间回测被压成 50 根而不自知
    closes = []
    bar_dates: list = []
    dates_complete = True
    for b in bars if bars else []:
        if not isinstance(b, dict):
            continue
        c = b.get("close")
        if c is None:
            continue
        try:
            v = float(c)
        except (TypeError, ValueError):
            continue
        # NaN check
        if v != v:
            continue
        closes.append(v)
        # 同步收集真实 bar 日期：as_of 推断必须落在最后一根真实 K 线上，
        # 不能拿“start + bar 数”的伪日期冒充（否则 PIT 基准日与数据错位）。
        _raw_d = b.get("date")
        _ts = None
        if _raw_d is not None:
            try:
                _ts = pd.Timestamp(str(_raw_d)[:10])
            except (ValueError, TypeError):
                _ts = None
        if _ts is None or pd.isna(_ts):
            dates_complete = False
            bar_dates.append(None)
        else:
            bar_dates.append(_ts)
    if not closes:
        # 中文：拉取失败（断网/接口/解析错误）与“窗口内确实无数据”必须区分：
        # 前者把原因透出并 fail-closed，绝不用合成 bars 顶替（伪造行情）。
        if fetch_error:
            logger.warning("market bars fetch failed for %s %s->%s: %s", symbol, start, end, fetch_error)
            return {
                "equity": [],
                "metrics": {},
                "ok": False,
                "error": f"market data fetch failed for {symbol} {start}->{end}: {fetch_error} (fail-closed, no synthetic bars)",
                "engine": engine or "default",
                "provenance": _prov_envelope("market", live_prov),
            }
        # 中文：无真实行情时默认走合成演示（存量契约：b3b/d2  pin ok:True + synthetic
        # provenance），但必须显式标记 synthetic + unit；显式 allow_synthetic=False
        # 时 fail-closed 返回 {ok:False}，杜绝合成被当真实回测用。
        if not allow_synthetic:
            logger.warning("no market bars for %s %s->%s, refusing synthetic fallback (allow_synthetic=False)", symbol, start, end)
            return {
                "equity": [],
                "metrics": {},
                "ok": False,
                "error": f"no market bars for {symbol} {start}->{end} (synthetic fallback requires allow_synthetic=True)",
                "engine": engine or "default",
                "provenance": _prov_envelope("synthetic"),
            }
        is_synthetic = True
        logger.warning("no market bars for %s %s->%s, using synthetic fallback", symbol, start, end)
        # Derive bar count from the requested start/end+freq instead of a fixed
        # 3-point series, so equity length matches the query coverage.
        _freq_map_fb = {"1d": "D", "1h": "h", "1m": "min", "1w": "W", "1M": "M"}
        _freq_fb = _freq_map_fb.get(interval or "1d", "D")
        try:
            _idx_tmp = pd.date_range(start, end, freq=_freq_fb)
            _n = max(len(_idx_tmp), 1)
        except (ValueError, TypeError):
            _n = 3
        closes = [100.0 + i for i in range(_n)]
    else:
        is_synthetic = False
    # DatetimeIndex — 真实 bar 日期优先（interval aware 的 date_range 仅作兜底）
    freq_map = {"1d": "D", "1h": "h", "1m": "min", "1w": "W", "1M": "M"}
    freq = freq_map.get(interval or "1d", "D")
    if not is_synthetic and dates_complete and bar_dates:
        idx = pd.DatetimeIndex(bar_dates)
    else:
        try:
            idx = pd.date_range(start, periods=len(closes), freq=freq)
        except (ValueError, TypeError) as e:
            logger.warning("date_range start parse failed: %s", e, exc_info=True)
            idx = pd.date_range("2026-08-01", periods=len(closes), freq=freq)

    # 多资产价格构造
    need_multi = len(weights) > 1
    prices = None
    if is_comma_symbol:
        # 中文：tickers-vs-weights 错位已在拉取前拦截，这里只构造多资产矩阵
        # 合成每标的的 close 序列 — any matrix built from synthetic prices is
        # synthetic regardless of the initial fetch result (provenance honesty)
        price_dict: dict[str, pd.Series] = {}
        for t in tickers:
            df_syn = _synthetic_prices_for_backtest(idx, t)
            price_dict[t] = df_syn["close"]
        prices = pd.DataFrame(price_dict, index=idx)
        is_synthetic = True
        # 补充 open 列为首资产的 open — but do not leak into price matrix for engine
        # keep auxiliary separate and drop before run
    elif need_multi and not is_comma_symbol:
        # 单 symbol 但多权重的 fallback：检测是否已有单列价格需要扩展为多列
        logger.warning("single price column with %d weights: constructing synthetic multi-asset price matrix for honest backtest", len(weights))
        price_dict = {}
        for i, wi in enumerate(weights):
            t = f"{symbol}_{i}"
            df_syn = _synthetic_prices_for_backtest(idx, t)
            price_dict[f"asset_{i}"] = df_syn["close"]
        prices = pd.DataFrame(price_dict, index=idx)
        is_synthetic = True
    else:
        # 单资产路径
        prices = pd.DataFrame({"close": closes}, index=idx)

    # Ensure open column not leaked into multi-asset matrix
    if prices is not None and "open" in prices.columns:
        prices = prices.drop(columns=["open"], errors="ignore")

    # ---- PIT 基准日：显式 as_of 优先，缺省推断为最后一根 K 线 ----
    inferred_as_of = as_of is None
    if as_of is None:
        price_date = prices.index[-1]
    else:
        try:
            price_date = pd.Timestamp(as_of)
        except (ValueError, TypeError) as e:
            logger.warning("run_backtest invalid as_of %r: %s", as_of, e)
            return {
                "equity": [],
                "metrics": {},
                "ok": False,
                "error": f"invalid as_of {as_of!r}: {e}",
                "engine": engine or "default",
                "provenance": _prov_envelope("synthetic" if is_synthetic else "market", live_prov),
            }
        if price_date > prices.index[-1]:
            logger.warning("run_backtest as_of %s is after last bar %s", as_of, prices.index[-1])
            return {
                "equity": [],
                "metrics": {},
                "ok": False,
                "error": (
                    f"as_of {as_of} is after last bar {prices.index[-1]} "
                    f"for {symbol} (future data, fail-closed)"
                ),
                "engine": engine or "default",
                "provenance": _prov_envelope("synthetic" if is_synthetic else "market", live_prov),
            }

    try:
        from hero_quant.backtest.engine import BacktestEngine

        eng = BacktestEngine()
        # 合成 PIT opt-in 需工具显式 allow_synthetic=True（防 is_synthetic 自动旁路 PIT）
        _eng_kw = {"allow_synthetic": True} if (is_synthetic and allow_synthetic) else {}
        # 真实行情必须显式给 price_date，否则 engine.py 的 PIT fail-closed 恒抛
        _eng_kw["price_date"] = price_date
        if weights_on is not None:
            _eng_kw["weights_on"] = weights_on
        res = eng.run(prices, weights=weights, costs=float(costs) if costs is not None else 0.0005, engine=engine or "default", **_eng_kw)
    except (ValueError, RuntimeError) as e:
        logger.warning("run_backtest engine failed: %s", e, exc_info=True)
        return {"equity": [], "metrics": {}, "ok": False, "error": str(e), "engine": engine or "default", "provenance": _prov_envelope("synthetic" if is_synthetic else "market", live_prov)}
    except Exception as e:
        logger.warning("run_backtest unexpected failed: %s", e, exc_info=True)
        return {"equity": [], "metrics": {}, "ok": False, "error": f"{type(e).__name__}: {e}", "engine": engine or "default", "provenance": _prov_envelope("synthetic" if is_synthetic else "market", live_prov)}

    eq = res.get("equity")
    if hasattr(eq, "tolist"):
        equity = eq.tolist()
    elif hasattr(eq, "values"):
        equity = list(eq.values)  # type: ignore
    else:
        equity = list(eq) if isinstance(eq, (list, tuple)) else []
    # 中文：provenance 必传——合成兜底标 synthetic，否则沿 loader 口径（含 unit）
    provenance = _prov_envelope("synthetic" if is_synthetic else "market", live_prov)
    data_synthetic = is_synthetic or provenance.get("source") == "synthetic"
    # PIT 口径诚实化：合成数据 / 推断出的 as_of 都不是调用方声明的 PIT 时点，
    # 必须 non_pit=True；只有显式 as_of 才沿用引擎判定。
    if data_synthetic:
        pit_basis = "synthetic"
        non_pit = True
    elif inferred_as_of:
        pit_basis = "inferred_as_of"
        non_pit = True
    else:
        pit_basis = "explicit_as_of"
        non_pit = bool(res.get("non_pit", False))
    metrics = dict(res.get("metrics") or {})
    metrics["non_pit"] = non_pit
    return {
        "equity": equity,
        "metrics": metrics,
        "ok": True,
        "engine": engine or "default",
        "provenance": provenance,
        "non_pit": non_pit,
        "pit_basis": pit_basis,
    }


@tool(
    name="validate_backtest",
    description="Validate PIT correctness for backtest inputs.",
    parameters={
        "type": "object",
        "properties": {
            "weights_on": {"type": "string"},
            "price_date": {"type": "string"},
        },
        "required": ["weights_on", "price_date"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {"valid": {"type": "boolean"}, "ok": {"type": "boolean"}, "error": {"type": "string"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: True,
)
def validate_backtest(weights_on: str, price_date: str) -> Dict[str, Any]:
    """校验回测 PIT 正确性（权重日期不得晚于行情日期）。 PIT: weights_on must be <= price_date"""
    try:
        from hero_quant.backtest.validation import ValidationError, validate

        import pandas as pd

        prices = pd.DataFrame({"close": [100, 101]}, index=pd.date_range(price_date, periods=2))
        validate(prices, weights_on=weights_on, price_date=price_date)
        return {"valid": True, "ok": True}
    except (ValidationError, ValueError, TypeError, AttributeError, RuntimeError) as e:
        logger.warning("validate_backtest failed: %s", e, exc_info=True)
        return {"valid": False, "ok": False, "error": str(e)}


@tool(
    name="get_backtest_metrics",
    description="Compute metrics for equity curve (Sharpe, drawdown, annual).",
    parameters={
        "type": "object",
        "properties": {"equity": {"type": "array"}},
        "required": ["equity"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {"metrics": {"type": "object"}, "ok": {"type": "boolean"}, "error": {"type": "string"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: True,
)
def get_backtest_metrics(equity: list) -> Dict[str, Any]:
    """基于净值曲线计算 Sharpe、回撤等回测指标。"""
    try:
        import pandas as pd
        from hero_quant.backtest.metrics import compute_metrics

        if equity is None or len(equity) == 0:
            return {"metrics": {}, "ok": False, "error": "equity is empty"}
        s = pd.Series(equity)
        m = compute_metrics(s)
        return {"metrics": m, "ok": True}
    except (ValueError, TypeError, AttributeError, KeyError, IndexError, ZeroDivisionError, RuntimeError) as e:
        logger.warning("get_backtest_metrics failed: %s", e, exc_info=True)
        return {"metrics": {}, "ok": False, "error": str(e)}


@tool(
    name="list_backtest_engines",
    description="List available backtest engines.",
    parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    output={
        "type": "object",
        "properties": {"engines": {"type": "array"}, "ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: True,
)
def list_backtest_engines() -> Dict[str, Any]:
    """列出可用回测引擎。"""
    return {"engines": ["default", "vectorized", "synthetic"], "ok": True}


@tool(
    name="optimize_portfolio",
    description="Simple portfolio weight optimizer placeholder (equal weight).",
    parameters={
        "type": "object",
        "properties": {
            "symbols": {"type": "array"},
            "method": {"type": "string"},
        },
        "required": ["symbols"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {"weights": {"type": "array"}, "ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
    is_concurrency_safe=lambda args: False,
)
def optimize_portfolio(symbols: list, method: str = "equal") -> Dict[str, Any]:
    """投资组合权重优化占位：当前返回等权配置；空 symbols 走 {ok:False} 信封。"""
    # 中文：空组合 fail-closed——返回错位 weights=[1.0] 会破坏 tickers-vs-weights 对齐校验
    if not symbols:
        return {"weights": [], "ok": False, "method": method, "error": "symbols is empty"}
    n = len(symbols)
    w = [1.0 / n] * n
    return {"weights": w, "ok": True, "method": method}
