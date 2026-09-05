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
                # 中文：委托返回缺 non-PIT 标记时补上，不直接透传伪装 PIT
                if "non-PIT" not in txt and "non-pit" not in txt.lower():
                    return txt.rstrip() + " [non-PIT source/unavailable]"
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
    *,
    _resolved_map: dict | None = None,
    _resolved_ticker: str | None = None,
    _resolved: bool = False,
) -> str:
    """按后缀映射为 ticker 解析对应区域基准；显式基准优先。

    _resolved=True 时直接用已解析的 _resolved_map/_resolved_ticker，不重建 Settings
   （run_batch 循环内复用，避免每 ticker 重复构造）。
    """
    if _resolved:
        if _resolved_ticker:
            return _resolved_ticker
        bmap = _resolved_map if _resolved_map is not None else dict(DEFAULT_BENCHMARK_MAP)
        tu = str(ticker).upper()
        for suffix, bench in sorted(bmap.items(), key=lambda kv: len(kv[0]), reverse=True):
            if suffix and tu.endswith(suffix.upper()):
                return bench
        return bmap.get("", "SPY")
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
    # 中文：fail-closed 前置——合成价必须显式 opt-in，空输入也不静默返回 {}
    # NOTE: this harness is synthetic-only by design; real-price runs belong to
    # BacktestEngine/tools with market provenance, not to this batch helper.
    if not allow_synthetic:
        raise ValueError("bench run_batch is synthetic-only and requires allow_synthetic=True (fail-closed)")
    if not tickers:
        raise ValueError("bench run_batch requires non-empty tickers (fail-closed)")
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
        # 中文：复用循环外已解析的基准（_resolved），不每 ticker 重建 Settings
        bench = _resolve_benchmark(t, _resolved_map=_cached_bmap, _resolved_ticker=_cached_bench_ticker, _resolved=True)
        prices = _synthetic_prices(idx, t)
        bench_prices = _synthetic_prices(idx, bench)

        engine = BacktestEngine()
        _engine_kwargs = {"allow_synthetic": True}
        try:
            res = engine.run(prices, **_engine_kwargs)
        except (ValueError, RuntimeError) as e:
            # 中文：策略腿失败直接传播（fail-closed），不伪装零收益；基准腿才标记兜底
            logger.warning("engine run failed for %s: %s", t, e, exc_info=True)
            raise
        except Exception as e:
            logger.error("unexpected engine run failure for %s: %s", t, e, exc_info=True)
            raise
        try:
            bench_res = engine.run(bench_prices, **_engine_kwargs)
        except (ValueError, RuntimeError) as e:
            logger.warning("engine bench run failed for %s (%s): %s", t, bench, e, exc_info=True)
            bench_res = {"metrics": {"cumulative_return": 0.0, "benchmark_error": str(e)[:500], "failed": True}, "failed": True}
        except Exception as e:
            logger.error("unexpected bench engine failure for %s (%s): %s", t, bench, e, exc_info=True)
            raise

        strat_metrics = dict(res.get("metrics", {}))
        _bench_failed = bool(bench_res.get("failed")) or "benchmark_error" in bench_res.get("metrics", {})

        def _safe_cum(d, default=0.0):
            """Coerce cumulative_return without aborting the batch on bad values.

            None, unparseable, or non-finite values mark the leg failed
            (alpha=None) instead of aborting the batch — and never coerce to
            a valid-looking 0.0.
            """
            try:
                raw = d.get("cumulative_return", default)
                if raw is None:
                    return None
                if isinstance(raw, np.ndarray):
                    if raw.size != 1:
                        return None
                    raw = raw.flat[0]
                v = float(raw)
            except (TypeError, ValueError, AttributeError):
                return None
            return v if np.isfinite(v) else None

        bench_cum = _safe_cum(bench_res.get("metrics", {}))
        strat_cum = _safe_cum(strat_metrics)
        if strat_cum is None or bench_cum is None:
            # mark failed / alpha=None instead of aborting batch with partial loss
            _bench_failed = True
            bench_cum = bench_cum if bench_cum is not None else 0.0
            strat_cum = strat_cum if strat_cum is not None else 0.0
        # 中文：基准腿失败时 alpha 置 None，不可用 0.0 伪装有效值
        alpha = float(strat_cum - bench_cum) if not _bench_failed else None

        # 丰富指标：注入基准与 alpha 字段便于对比
        enriched = dict(strat_metrics)
        enriched["benchmark"] = bench
        enriched["benchmark_return"] = bench_cum
        enriched["alpha"] = alpha
        if _bench_failed:
            enriched["benchmark_error"] = bench_res.get("metrics", {}).get("benchmark_error", "benchmark leg failed")
            enriched["failed"] = enriched.get("failed", False) or True
        if res.get("failed"):
            enriched["failed"] = True
            if res.get("error"):
                enriched["error"] = res["error"]
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
        # 保证 JSON 可序列化：递归转换 numpy 标量/数组（含嵌套结构）
        def _jsonable(v):
            if isinstance(v, (np.floating, np.integer)):
                return float(v)
            if isinstance(v, np.ndarray):
                return float(v) if v.size == 1 else [_jsonable(x) for x in v.tolist()]
            if isinstance(v, dict):
                return {k: _jsonable(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [_jsonable(x) for x in v]
            return v

        for k, v in list(enriched.items()):
            enriched[k] = _jsonable(v)

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
            # no ".." and relative — multi-component paths (e.g. a/link_to_etc
            # where a/link is a symlink outside CWD) must also be contained:
            # the resolved target must stay within _base (or tmpdir).
            if not (_is_within(_target, _base) or _is_within(_target, _tmpdir)):
                raise ValueError(f"output_dir traversal detected: {output_dir!r} escapes {_base}")
            # 中文：safe_join 的 ValueError 是拒绝信号，必须传播；仅 import/类型问题可跳过
            try:
                from hero_quant.security.sanitize import safe_join as _safe_join  # type: ignore
            except ImportError:
                _safe_join = None  # type: ignore[assignment]
            if _safe_join is not None and len(_p.parts) == 1 and _p.suffix.lower() != ".json":
                try:
                    _safe_join(_base, _p.name)
                except ValueError:
                    raise
                except (TypeError, AttributeError, OSError) as e:
                    logger.debug("output_dir safe_join check skipped: %s", e)
        # 中文：写经校验的解析目标 _target，不用未解析的 out（防 symlink TOCTOU）
        out = _target
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
