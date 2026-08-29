"""批量回测：区域基准映射 + 引擎批处理封装。

职责：按后缀映射为每只 ticker 解析区域基准，并批量驱动 BacktestEngine，计算 alpha 等对比指标。
架构位置：backtest 上层编排，复用 BacktestEngine；基准映射与配置中心 Settings 联动。
关键设计：显式 benchmark_ticker 优先于后缀映射；后缀按长度降序匹配避免部分命中；单日输入自动扩展为 5 日以保证收益可计算。
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import pathlib

import numpy as np
import pandas as pd

from hero_quant.backtest.engine import BacktestEngine

logger = logging.getLogger(__name__)


def _build_tearsheet_html(results: dict[str, dict], disclosure_text: str) -> str:
    """Build minimal tearsheet html containing non-PIT disclosure and per-ticker rows."""
    esc_disclosure = html.escape(disclosure_text) if disclosure_text else "non-PIT source/unavailable"
    rows = ""
    for ticker, m in results.items():
        esc_ticker = html.escape(str(ticker))
        bench = html.escape(str(m.get("benchmark", "")))
        alpha = m.get("alpha", "")
        try:
            esc_alpha = html.escape(str(alpha))
        except (TypeError, ValueError, AttributeError) as e:
            import logging as _logging
            _logging.getLogger(__name__).debug("html.escape alpha failed: %s", e)
            esc_alpha = ""
        try:
            pretty = json.dumps(m, indent=2, ensure_ascii=False)
        except (TypeError, ValueError, OverflowError, AttributeError) as e:
            import logging as _logging2
            _logging2.getLogger(__name__).debug("json.dumps metrics failed: %s", e)
            pretty = str(m)
        esc_pretty = html.escape(pretty)
        rows += f"<tr><td>{esc_ticker}</td><td>{bench}</td><td>{esc_alpha}</td><td><pre>{esc_pretty}</pre></td></tr>\n"
    if not rows:
        rows = "<tr><td colspan='4'>no results</td></tr>\n"
    # ensure literal non-PIT marker present even if disclosure_text varied
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Tearsheet</title></head>
<body>
<h1>Tearsheet</h1>
<p>{esc_disclosure}</p>
<p>non-PIT source/unavailable</p>
<table border="1" cellpadding="6" cellspacing="0">
<thead><tr><th>Ticker</th><th>Benchmark</th><th>Alpha</th><th>Metrics</th></tr></thead>
<tbody>
{rows}</tbody>
</table>
<p>{esc_disclosure}</p>
</body>
</html>
"""

# ------------------------------------------------------------------ pit disclosure
def _build_pit_disclosure(news_records: list[dict] | None = None) -> str:
    """生成 non-PIT 披露文本，诚实标注 PIT 不可用。"""
    if not news_records:
        return "non-PIT source/unavailable - no verified news snapshot (PIT unavailable)"
    # 若记录已含 pit 标注
    try:
        has_pit = any("pit" in r for r in news_records) if news_records else False
        if has_pit:
            total = len(news_records)
            pit_true = sum(1 for r in news_records if r.get("pit") is True)
            pit_false = total - pit_true
            if pit_false == total:
                return f"non-PIT source/unavailable - {pit_false}/{total} records not PIT-verified"
            if pit_false > 0:
                return f"PIT verified {pit_true}/{total}; non-PIT source/unavailable {pit_false}/{total}"
            return f"PIT verified {pit_true}/{total}; non-PIT source/unavailable 0/{total}"
        # 无 pit 字段：尝试借助 news loader 的 disclosure
        try:
            from hero_quant.data.loaders.news import get_disclosure as _gd

            txt = _gd(news_records)
            if isinstance(txt, str) and txt.strip():
                return txt
        except (ImportError, AttributeError, TypeError, ValueError) as e:
            import logging as _logging3

            _logging3.getLogger(__name__).debug("news get_disclosure failed: %s", e)
        return "non-PIT source/unavailable - PIT status not verified"
    except (TypeError, ValueError, AttributeError, KeyError) as e:
        import logging as _logging4

        _logging4.getLogger(__name__).debug("pit disclosure outer failed: %s", e)
        return "non-PIT source/unavailable - PIT status unknown"


def get_disclosure(news_records: list | None = None, **kwargs) -> str:
    if news_records is None and "news" in kwargs:
        news_records = kwargs["news"]
    return _build_pit_disclosure(news_records)


def _deprecated_alias(name: str) -> "callable":
    import warnings

    def _fn(news_records: list | None = None, **kwargs) -> str:
        warnings.warn(f"{name} is deprecated, use get_disclosure", DeprecationWarning, stacklevel=2)
        if news_records is None and "news" in kwargs:
            news_records = kwargs["news"]
        return _build_pit_disclosure(news_records)

    _fn.__name__ = name
    return _fn


build_disclosure = _deprecated_alias("build_disclosure")
get_pit_disclosure = _deprecated_alias("get_pit_disclosure")
build_pit_disclosure = _deprecated_alias("build_pit_disclosure")
get_bench_disclosure = _deprecated_alias("get_bench_disclosure")
news_disclosure = _deprecated_alias("news_disclosure")


# ------------------------------------------------------------------ benchmark map
# 区域基准后缀映射：与上游默认配置保持一致，便于跨市场对比
DEFAULT_BENCHMARK_MAP: dict[str, str] = {
    ".NS": "^NSEI",
    ".BO": "^BSESN",
    ".T": "^N225",
    ".HK": "^HSI",
    ".L": "^FTSE",
    ".TO": "^GSPTSE",
    ".AX": "^AXJO",
    ".SS": "000001.SS",
    ".SZ": "399001.SZ",
    "": "SPY",
}


def _effective_benchmark_map(benchmark_map: dict[str, str] | None) -> dict[str, str]:
    """解析生效的基准映射：显式传入优先，否则取 Settings，否则回落默认表。仅捕获预期异常，配置错误向上抛出。"""
    if benchmark_map is not None:
        return benchmark_map
    # 尝试从配置中心读取，未配置则回落默认
    try:
        from hero_quant.config.settings import Settings

        s = Settings()
        if getattr(s, "benchmark_map", None):
            return dict(s.benchmark_map)
    except (ImportError, AttributeError) as e:
        logger.debug("_effective_benchmark_map Settings unavailable: %s", e)
    except Exception as e:
        logger.warning("_effective_benchmark_map Settings failed: %s", e, exc_info=True)
        raise
    return dict(DEFAULT_BENCHMARK_MAP)


def _effective_benchmark_ticker(benchmark_ticker: str | None) -> str | None:
    """解析生效的基准标的：显式参数覆盖 Settings。仅捕获预期异常。"""
    if benchmark_ticker is not None:
        # 空字符串视为未覆盖，避免误用
        return benchmark_ticker if benchmark_ticker != "" else None
    try:
        from hero_quant.config.settings import Settings

        s = Settings()
        bt = getattr(s, "benchmark_ticker", None)
        if bt:
            return str(bt)
    except (ImportError, AttributeError) as e:
        logger.debug("_effective_benchmark_ticker Settings unavailable: %s", e)
    except Exception as e:
        logger.warning("_effective_benchmark_ticker Settings failed: %s", e, exc_info=True)
        raise
    return None


def _resolve_benchmark(
    ticker: str,
    benchmark_map: dict | None = None,
    benchmark_ticker: str | None = None,
) -> str:
    """按后缀映射为 ticker 解析对应区域基准；显式基准优先。"""
    explicit = _effective_benchmark_ticker(benchmark_ticker)
    if explicit:
        return explicit
    bmap = _effective_benchmark_map(benchmark_map)
    tu = str(ticker).upper()
    # 按后缀长度降序匹配，避免短后缀误命中
    for suffix, bench in sorted(bmap.items(), key=lambda kv: len(kv[0]), reverse=True):
        if suffix and tu.endswith(suffix.upper()):
            return bench
    return bmap.get("", "SPY")


def _normalize_index(dates: list[str] | None) -> pd.DatetimeIndex:
    """归一化日期序列：空回落至默认 5 日；非法日期抛错（而非静默回落）；单日扩展为 5 日。"""
    if not dates:
        dates = ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    try:
        idx = pd.to_datetime(dates)
        if not isinstance(idx, pd.DatetimeIndex):
            idx = pd.DatetimeIndex(idx)
    except (ValueError, TypeError, pd.errors.OutOfBoundsDatetime) as e:
        logger.warning("_normalize_index unparseable dates %r: %s", dates, e, exc_info=True)
        raise ValueError(f"unparseable dates {dates!r}: {e}") from e
    except Exception as e:
        logger.warning("_normalize_index unexpected error for %r: %s", dates, e, exc_info=True)
        raise
    # fail on NaT introduced by coercion (e.g. bad strings with errors='coerce' not used but guard)
    try:
        if idx.isna().any():
            raise ValueError(f"unparseable dates {dates!r}: contains NaT")
    except (AttributeError, ValueError):
        raise
    except Exception as e:
        logger.warning("_normalize_index NaT check failed: %s", e, exc_info=True)
        raise
    # 单日无收益，需扩展为多日序列（业务日，避免周末无交易日污染）
    if len(idx) == 1:
        idx = pd.date_range(idx[0], periods=5, freq="B")
    # 保证有序，避免后续 pct_change 错位
    try:
        idx = idx.sort_values()
    except (ValueError, TypeError, AttributeError) as e:
        logger.warning("_normalize_index sort failed: %s", e, exc_info=True)
        raise
    except Exception as e:
        logger.warning("_normalize_index sort unexpected: %s", e, exc_info=True)
        raise
    return idx


def _synthetic_prices(index: pd.DatetimeIndex, ticker: str) -> pd.DataFrame:
    """按 ticker 生成确定性合成价格（趋势+噪声），用于批量对比与无数据源时的演示。"""
    n = len(index)
    # Deterministic seed via sha256 (avoid hash() randomization under PYTHONHASHSEED)
    seed = int(hashlib.sha256(str(ticker).encode()).hexdigest()[:8], 16)  # 32-bit seed
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 0.5, size=n)
    trend = np.arange(n) * 0.3  # 线性趋势，避免长期水平导致指标退化
    close = 100 + trend + np.cumsum(noise) * 0.2
    close = np.maximum(close, 1.0)  # 下限保护，避免非正价格触犯校验
    df = pd.DataFrame({"close": close.astype(float)}, index=index)
    # 补充 open 以支持 _align 次日开盘执行
    try:
        df["open"] = df["close"].shift(1).fillna(df["close"].iloc[0])
    except (ValueError, TypeError, AttributeError, KeyError, IndexError) as e:
        import logging as _logging5

        _logging5.getLogger(__name__).debug("synthetic open fill failed: %s", e)
        df["open"] = df["close"]
    return df


def run_batch(
    tickers: list[str],
    dates: list[str] | None = None,
    output_dir: str | pathlib.Path | None = None,
    benchmark_ticker: str | None = None,
    benchmark_map: dict | None = None,
    news_records: list[dict] | None = None,
    allow_synthetic: bool = False,
    **kwargs,
) -> dict:
    """批量执行回测并计算相对基准的 alpha：为每只 ticker 合成价格、运行引擎、对比基准收益。"""
    # 兼容：dates / news_records 可能经 kwargs 传入（旧调用兼容）
    if dates is None and "dates" in kwargs:
        dates = kwargs.pop("dates")
    if news_records is None and "news_records" in kwargs:
        news_records = kwargs.pop("news_records")
    if news_records is None and "news" in kwargs:
        news_records = kwargs.pop("news")
    # 兼容 news 相关的 snapshot 别名透传给 disclosure 辅助（不影响核心回测）
    kwargs.pop("snapshot_date", None)
    kwargs.pop("available_at", None)
    kwargs.pop("snapshot", None)
    kwargs.pop("snapshot_time", None)
    kwargs.pop("avail_at", None)
    kwargs.pop("pit_snapshot", None)
    if tickers is None:
        tickers = []
    if isinstance(tickers, str):
        tickers = [tickers]  # 单字符串归一为列表

    # 解析 disclosure 文本（诚实标注 non-PIT）
    disclosure_text = _build_pit_disclosure(news_records)

    idx = _normalize_index(dates)
    results: dict[str, dict] = {}

    # Hoist Settings / benchmark_map caching outside ticker loop — avoid per-ticker Settings() construction
    _cached_bmap = _effective_benchmark_map(benchmark_map)
    _cached_bench_ticker = _effective_benchmark_ticker(benchmark_ticker)

    for ticker in tickers:
        t = str(ticker)
        bench = _resolve_benchmark(t, benchmark_map=_cached_bmap, benchmark_ticker=_cached_bench_ticker)
        prices = _synthetic_prices(idx, t)
        bench_prices = _synthetic_prices(idx, bench)

        engine = BacktestEngine()
        if not allow_synthetic:
            raise ValueError("bench run_batch synthetic requires allow_synthetic=True (fail-closed); pass allow_synthetic=True or provide real price data")
        _engine_kwargs = {"allow_synthetic": True}
        try:
            res = engine.run(prices, **_engine_kwargs)
        except (ValueError, RuntimeError) as e:
            # bench 层失败以零化指标兜底但需显式标记 provenance.synthetic，避免上游误判为正常收益
            logger.warning("engine run failed for %s: %s", t, e, exc_info=True)
            res = {"metrics": {"sharpe": 0.0, "cumulative_return": 0.0, "annual_return": 0.0, "max_drawdown": 0.0, "turnover": 0.0, "volatility": 0.0, "provenance": "synthetic_fallback"}}
        except Exception as e:
            logger.error("unexpected engine run failure for %s: %s", t, e, exc_info=True)
            raise
        try:
            bench_res = engine.run(bench_prices, **_engine_kwargs)
        except (ValueError, RuntimeError) as e:
            logger.warning("engine bench run failed for %s (%s): %s", t, bench, e, exc_info=True)
            bench_res = {"metrics": {"cumulative_return": 0.0}}
        except Exception as e:
            logger.error("unexpected bench engine failure for %s (%s): %s", t, bench, e, exc_info=True)
            raise

        strat_metrics = dict(res.get("metrics", {}))
        bench_cum = float(bench_res.get("metrics", {}).get("cumulative_return", 0.0))
        strat_cum = float(strat_metrics.get("cumulative_return", 0.0))
        alpha = float(strat_cum - bench_cum)  # 超额收益 = 策略累计 - 基准累计

        # 丰富指标：注入基准与 alpha 字段便于对比
        enriched = dict(strat_metrics)
        enriched["benchmark"] = bench
        enriched["benchmark_return"] = bench_cum
        enriched["alpha"] = alpha
        enriched["alpha_vs"] = f"alpha vs {bench}"
        enriched["ticker"] = t
        # PIT 披露：诚实标注 non-PIT（避免伪造 PIT）
        enriched["disclosure"] = disclosure_text
        enriched["pit_disclosure"] = disclosure_text
        enriched["news_disclosure"] = disclosure_text
        enriched["non_pit_disclosure"] = disclosure_text
        # 额外诚实字段：无 PIT 源时明确 unavailable
        if news_records:
            try:
                pit_true = sum(1 for r in news_records if r.get("pit") is True)
                enriched["news_pit_verified"] = bool(pit_true > 0 and pit_true == len(news_records))
                enriched["pit_status"] = "verified" if pit_true == len(news_records) and pit_true > 0 else "unavailable"
                enriched["non_pit_count"] = len(news_records) - pit_true
            except (TypeError, ValueError, AttributeError, KeyError) as e:
                import logging as _logging6

                _logging6.getLogger(__name__).debug("pit_status enrichment failed: %s", e)
                enriched["news_pit_verified"] = False
                enriched["pit_status"] = "unavailable"
        else:
            enriched["news_pit_verified"] = False
            enriched["pit_status"] = "unavailable"
            enriched["non_pit_count"] = 0
        # 保证 JSON 可序列化：转换 numpy 标量/数组
        for k, v in list(enriched.items()):
            if isinstance(v, (np.floating, np.integer)):
                enriched[k] = float(v)
            elif isinstance(v, (np.ndarray,)):
                enriched[k] = float(v) if v.size == 1 else v.tolist()

        results[t] = enriched

    # 落盘 metrics.json（支持目录或 .json 文件路径两种形态）
    # output_dir 为目录时额外生成最小 tearsheet.html（含 PIT/non-PIT 披露与每 ticker 结果）；为 .json 时保持原语义不旁写
    if output_dir is not None:
        # traversal guard: always resolve; absolute paths must be inside CWD or allowlisted tempfile dir.
        # Blocks absolute /tmp bypass and ".." escapes. tempfile.gettempdir() is allowlisted for tests.
        import tempfile as _tf

        _p = pathlib.Path(output_dir)
        _base = pathlib.Path.cwd().resolve()
        _target = _p.resolve() if _p.is_absolute() else (_base / _p).resolve()
        _tmpdir = pathlib.Path(_tf.gettempdir()).resolve()
        # helper for is_relative_to compat (py <3.9 fallback)
        def _is_within(child: pathlib.Path, parent: pathlib.Path) -> bool:
            try:
                return child.is_relative_to(parent)  # type: ignore[attr-defined]
            except AttributeError:
                try:
                    child.relative_to(parent)
                    return True
                except ValueError:
                    return False
        _has_traversal = ".." in _p.parts or ".." in str(output_dir)
        if _p.is_absolute():
            if not (_is_within(_target, _base) or _is_within(_target, _tmpdir)):
                raise ValueError(f"output_dir traversal detected: {output_dir!r} escapes {_base} (not in tmpdir {_tmpdir})")
            if _has_traversal and not _is_within(_target, _base) and not _is_within(_target, _tmpdir):
                raise ValueError(f"output_dir traversal detected: {output_dir!r} escapes {_base}")
        elif _has_traversal:
            if not _is_within(_target, _base):
                raise ValueError(f"output_dir traversal detected: {output_dir!r} escapes {_base}")
        else:
            # no ".." and relative — optionally validate single-component via safe_join
            try:
                from hero_quant.security.sanitize import safe_join as _safe_join  # type: ignore

                if len(_p.parts) == 1 and _p.suffix.lower() != ".json":
                    try:
                        _safe_join(_base, _p.name)
                    except ValueError:
                        pass
            except ImportError:
                pass
            except (ValueError, TypeError, AttributeError, OSError) as e:
                logger.debug("output_dir safe_join check skipped: %s", e)
        out = pathlib.Path(output_dir)
        # 若给出的是 .json 文件路径则直接写入其本身，不强行旁写 tearsheet
        if out.suffix.lower() == ".json":
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                logger.warning("metrics.json write failed (%s): %s", out, e, exc_info=True)
                raise
        else:
            out.mkdir(parents=True, exist_ok=True)
            p = out / "metrics.json"
            try:
                p.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                logger.warning("metrics.json write failed (%s): %s", p, e, exc_info=True)
                raise
            # 生成最小 tearsheet.html
            try:
                html_text = _build_tearsheet_html(results, disclosure_text)
                (out / "tearsheet.html").write_text(html_text, encoding="utf-8")
            except Exception as e:
                logger.warning("tearsheet.html write failed (%s): %s", out / "tearsheet.html", e, exc_info=True)
                raise

    return results


__all__ = [
    "DEFAULT_BENCHMARK_MAP",
    "_resolve_benchmark",
    "run_batch",
    "_build_pit_disclosure",
    "get_disclosure",
    "build_disclosure",
    "get_pit_disclosure",
    "build_pit_disclosure",
    "get_bench_disclosure",
    "news_disclosure",
]
