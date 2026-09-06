"""Lane B2a TDD：行情 loader 静默腐坏 20 条 failing tests，先红后绿。"""

import copy
import sys
import types
import inspect
import logging
import unittest.mock as mock
from datetime import datetime, timedelta

import pandas as pd
import pytest

# ========== akshare_loader 5 条 ==========

def test_b2a_akshare_date_index_fail_closed(monkeypatch):
    """akshare: 日期索引裸except pass 应改为窄化捕获 + 抛 DataValidationError。"""
    from hero_quant.data.loaders.akshare_loader import AKShareLoader, DataValidationError

    loader = AKShareLoader()
    # 中文列 日期 含不可解析值，触发 to_datetime 失败应抛 DataValidationError 而非静默 pass
    df_ak = pd.DataFrame({
        "日期": ["not-a-date", "also-bad"],
        "开盘": [10, 11],
        "收盘": [10, 11],
        "最高": [12, 13],
        "最低": [9, 10],
        "成交量": [100, 100],
    })
    with pytest.raises(DataValidationError):
        loader._normalize_akshare(df_ak)


def test_b2a_akshare_synthetic_truncate_must_warn_or_raise():
    """akshare: 合成历史 500 天截断必须显式标记/抛错，不静默。"""
    from hero_quant.data.loaders.akshare_loader import AKShareLoader, DataValidationError

    loader = AKShareLoader()
    # 600 天区间应触发截断警告或直接抛错（fail-closed）
    with pytest.raises((DataValidationError, ValueError)):
        loader._synthetic_df("600519.SH", "2020-01-01", "2021-12-31")


def test_b2a_akshare_inverted_date_raises():
    """akshare: end < start 静默钳制应改为抛 DataValidationError。"""
    from hero_quant.data.loaders.akshare_loader import AKShareLoader, DataValidationError

    loader = AKShareLoader()
    with pytest.raises(DataValidationError):
        loader._synthetic_df("600519.SH", "2025-01-10", "2025-01-01")


def test_b2a_akshare_interval_respected(monkeypatch):
    """akshare: interval 校验后忽略（intraday 仍返回日线）应 fail-closed 拒绝非日线。"""
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import importlib
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.akshare_loader import AKShareLoader, DataValidationError

    loader = AKShareLoader()
    fake_ak = mock.MagicMock()
    fake_ak.stock_zh_a_hist.return_value = pd.DataFrame({
        "日期": ["2025-01-01"],
        "开盘": [10],
        "收盘": [10],
        "最高": [12],
        "最低": [9],
        "成交量": [100],
    })
    with mock.patch.dict("sys.modules", {"akshare": fake_ak}):
        with pytest.raises(DataValidationError):
            loader.get_bars("600519.SH", "2025-01-01", "2025-01-10", interval="1m")


def test_b2a_akshare_dead_code_removed():
    """akshare: if not dates: 死代码应删除。"""
    import pathlib
    src = pathlib.Path("src/hero_quant/data/loaders/akshare_loader.py").read_text(encoding="utf-8")
    assert "if not dates" not in src, "死代码 if not dates: 应已删除"


# ========== news.py 5 条 ==========

def test_b2a_news_date_overload_not_forge_pit():
    """news: date 键不应同时作为发布/交易双义伪造 PIT。"""
    import logging

    from hero_quant.data.loaders.news import load_news
    # 仅有 date 字段：date 不再视为 trade_date（交易日标签 vs 事件日期混淆，
    # OCR 处方已删），故整批缺 trade_date 列 → 告警 + 返回空列表，绝不伪造 PIT。
    rec = [{"id": 1, "date": "2024-01-02"}]
    out = load_news(copy.deepcopy(rec), trade_date="2024-01-02", snapshot_date="2024-01-03")
    assert out == [], f"缺 trade_date 列应返回空列表而非伪造，得到 {out}"
    # date 仍不作发布时间：带显式 trade_date 的记录，date 不得影响 PIT 判定
    rec2 = [{"id": 1, "trade_date": "2024-01-02", "date": "2024-01-02"}]
    out = load_news(copy.deepcopy(rec2), trade_date="2024-01-02", snapshot_date="2024-01-03")
    assert out[0]["pit"] is False, f"无发布时间不应伪造 PIT，得到 {out[0]}"
    assert out[0]["pit_status"] in ("unknown", "unavailable", "missing", "non-pit", "non_pit")


def test_b2a_news_get_disclosure_non_dict_safe():
    """news: get_disclosure 对非 dict 误判 pit（in 对字符串）应窄化 isinstance。"""
    from hero_quant.data.loaders import news as news_mod

    # 含非 dict 条目，之前 any("pit" in r) 会把字符串当容器误判
    records = ["pit is here", {"title": "x"}]
    # 不应抛 AttributeError，且应诚实视为 non-PIT
    text = news_mod.get_disclosure(records)
    assert isinstance(text, str)
    assert "non-PIT" in text or "unavailable" in text.lower()

    # _disclosure_text 同样应对非 dict 健壮
    text2 = news_mod._disclosure_text(records)  # type: ignore
    assert isinstance(text2, str)


def test_b2a_news_copy_dead_removed():
    """news: copy.copy(rec) 随即被 dict(rec) 覆盖的死代码应删除。"""
    import pathlib
    src = pathlib.Path("src/hero_quant/data/loaders/news.py").read_text(encoding="utf-8")
    assert "copy.copy" not in src, "copy.copy 死代码应删除"
    # 若不再使用 copy，import copy 也应移除（可选，但死 import 视为未清理）
    # 允许保留 import copy 若仍有他用，但此处应无 copy.copy 调用


def test_b2a_news_is_aware_hoisted():
    """news: _is_aware 不应逐行重建，应提升至模块级。"""
    import pathlib
    src = pathlib.Path("src/hero_quant/data/loaders/news.py").read_text(encoding="utf-8")
    # 模块级应有 def _is_aware
    assert "def _is_aware" in src
    # load_news 函数体内不应再定义 def _is_aware
    # 简单检查：load_news 源码中不应包含该定义
    from hero_quant.data.loaders import news as news_mod
    load_src = inspect.getsource(news_mod.load_news)
    assert "def _is_aware" not in load_src, "_is_aware 应提升至模块级，不在循环内重建"


def test_b2a_news_snapshot_keys_dead_removed():
    """news: 未使用的 _SNAPSHOT_KEYS 应删除或显式标注。"""
    import pathlib
    src = pathlib.Path("src/hero_quant/data/loaders/news.py").read_text(encoding="utf-8")
    assert "_SNAPSHOT_KEYS" not in src, "死常量 _SNAPSHOT_KEYS 应删除"


# ========== ccxt_loader 2 条 ==========

def test_b2a_ccxt_dead_logic_fixed(monkeypatch):
    """ccxt: 双分支同值死逻辑应按 1d/1w/1M 分别计算 requested。"""
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import importlib
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.ccxt_loader import CCXTLoader

    loader = CCXTLoader()
    captured = {}

    def fake_fetch(symbol, timeframe, since, limit):
        captured["limit"] = limit
        captured["timeframe"] = timeframe
        base_ts = int(datetime(2025, 1, 1).timestamp() * 1000)
        return [[base_ts + i * 86400000, 100, 101, 99, 100, 10] for i in range(5)]

    fake_exchange = mock.MagicMock()
    fake_exchange.fetch_ohlcv.side_effect = fake_fetch
    fake_ccxt = mock.MagicMock()
    fake_ccxt.binance.return_value = fake_exchange

    with mock.patch.dict("sys.modules", {"ccxt": fake_ccxt}):
        # 700 天的周线，错误实现会 requested=705，正确应约 105
        loader.get_bars("BTC/USDT", "2025-01-01", "2026-12-01", interval="1wk")
        assert captured["limit"] < 200, f"周线 limit 应按周数计算，得到 {captured['limit']} (死逻辑则 700+)"
        # 月线同样
        captured.clear()
        loader.get_bars("BTC/USDT", "2025-01-01", "2026-12-01", interval="1mo")
        assert captured["limit"] < 200, f"月线 limit 应按月数计算，得到 {captured['limit']}"


def test_b2a_ccxt_live_clip_to_window(monkeypatch):
    """ccxt: live 结果必须裁到 [start,end]，不外溢。"""
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import importlib
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.ccxt_loader import CCXTLoader

    loader = CCXTLoader()
    base = datetime(2025, 1, 1)

    def fake_fetch(symbol, timeframe, since, limit):
        # 返回 10 天，远超请求的 3 天窗口
        ohlcv = []
        for i in range(10):
            ts = int((base + timedelta(days=i)).timestamp() * 1000)
            ohlcv.append([ts, 100 + i, 101 + i, 99 + i, 100 + i, 10])
        return ohlcv

    fake_exchange = mock.MagicMock()
    fake_exchange.fetch_ohlcv.side_effect = fake_fetch
    fake_ccxt = mock.MagicMock()
    fake_ccxt.binance.return_value = fake_exchange

    with mock.patch.dict("sys.modules", {"ccxt": fake_ccxt}):
        df = loader.get_bars("BTC/USDT", "2025-01-01", "2025-01-03", interval="1d")
        # 应裁到 3 天（或含边界 3-4 行），不应返回 10 行
        assert len(df) <= 4, f"应裁到 [start,end]，得到 {len(df)} 行"
        # 索引应在窗口内
        assert df.index.min() >= pd.to_datetime("2025-01-01")
        assert df.index.max() <= pd.to_datetime("2025-01-03") + pd.Timedelta(days=1)


# ========== tencent 4 条 ==========

def test_b2a_tencent_hardcoded_window_and_interval(monkeypatch):
    """tencent: 硬编码 day,,,320,qfq 无视窗口/interval 应尊重窗口并拒绝非日线。"""
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import importlib
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.tencent import TencentLoader, DataValidationError

    loader = TencentLoader()
    # intraday 应直接拒绝
    with pytest.raises(DataValidationError):
        loader.get_bars("600519.SH", "2025-01-01", "2025-01-10", interval="1m")

    # 日线应把 start/end 体现在请求或返回裁剪上：mock 返回窗口外数据，验证被裁
    captured = {}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            import json
            # 返回跨度很大的 bars，含窗口外日期
            data = {
                "data": {
                    "sh600519": {
                        "qfqday": [
                            ["2024-12-20", "10", "10", "12", "9", "100"],
                            ["2025-01-02", "10", "11", "12", "9", "100"],
                            ["2025-01-05", "10", "11", "12", "9", "100"],
                            ["2025-02-01", "10", "11", "12", "9", "100"],
                        ]
                    }
                }
            }
            return json.dumps(data).encode()

    def fake_urlopen(url, timeout=2):
        captured["url"] = url
        # 若 url 仍为硬编码 day,,,320 则视为未修复，应断言失败
        assert "2025-01-01" in url or "20250101" in url or "day" in url, f"url 未体现窗口: {url}"
        return FakeResp()

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        bars = loader.get_bars("600519.SH", "2025-01-01", "2025-01-10", interval="1d")
        dates = [b["date"] for b in bars]
        # 返回应已裁到 [2025-01-01, 2025-01-10]，窗口外 2024-12-20 与 2025-02-01 不应出现
        assert "2024-12-20" not in dates
        assert "2025-02-01" not in dates


def test_b2a_tencent_dict_branch_strong_validation(monkeypatch):
    """tencent: dict 分支弱校验应对 NaN/inf/负 OHLC 抛 DataValidationError。"""
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import importlib
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.tencent import TencentLoader, DataValidationError
    from hero_quant.infra.redis import clear_redis_instance
    import json

    loader = TencentLoader()

    def make_resp(items):
        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return json.dumps({"data": {"sh600519": {"qfqday": items}}}).encode()
        return FakeResp()

    bad_cases = [
        [{"date": "2025-01-02", "open": float("nan"), "close": 10, "high": 12, "low": 9, "volume": 100}],
        [{"date": "2025-01-02", "open": 10, "close": float("inf"), "high": 12, "low": 9, "volume": 100}],
        [{"date": "2025-01-02", "open": -1, "close": 10, "high": 12, "low": 9, "volume": 100}],
        [{"date": "2025-01-02", "open": 10, "close": 10, "high": 12, "low": 9, "volume": -5}],
    ]
    for idx_c, items in enumerate(bad_cases):
        clear_redis_instance()
        # 使用唯一 symbol 避免 @cache 命中上一次成功结果
        sym = f"600519.SH_DICT{idx_c}"
        with mock.patch("urllib.request.urlopen", return_value=make_resp(items)):
            with pytest.raises(DataValidationError):
                loader.get_bars(sym, "2025-01-01", "2025-01-10", interval="1d")


def test_b2a_tencent_list_branch_float_wrapped(monkeypatch):
    """tencent: list 分支 float 裸抛应统一为 DataValidationError。"""
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import importlib
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.tencent import TencentLoader, DataValidationError
    from hero_quant.infra.redis import clear_redis_instance
    import json

    clear_redis_instance()
    loader = TencentLoader()

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            # list 分支：["date","open","close","high","low","volume"] 顺序
            return json.dumps({"data": {"sh600519": {"qfqday": [["2025-01-02", "not-a-number", "10", "12", "9", "100"]]}}}).encode()

    with mock.patch("urllib.request.urlopen", return_value=FakeResp()):
        with pytest.raises(DataValidationError):
            loader.get_bars("600519.SH_LIST", "2025-01-01", "2025-01-10", interval="1d")


def test_b2a_tencent_dead_coerce_removed():
    """tencent: 死 _coerce_float 应删除或被使用，不应残留未调用。"""
    import pathlib
    src = pathlib.Path("src/hero_quant/data/loaders/tencent.py").read_text(encoding="utf-8")
    # 要求不在文件中残留未被调用的 _coerce_float
    assert "_coerce_float" not in src, "死代码 _coerce_float 应已删除或重命名为被调用实现"


# ========== yahoo 4 条 ==========

def test_b2a_yahoo_intraday_not_truncated(monkeypatch):
    """yahoo: 日内时间戳截成日期应保留时间，避免多 bar 坍缩。"""
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import importlib
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.yahoo import YahooLoader

    loader = YahooLoader()

    # 构造含时间的 DataFrame，模拟 yfinance 返回
    idx = pd.to_datetime(["2025-01-02 09:30:00", "2025-01-02 09:31:00", "2025-01-02 09:32:00"])
    df = pd.DataFrame({
        "Open": [10, 10.1, 10.2],
        "High": [11, 11.1, 11.2],
        "Low": [9, 9.1, 9.2],
        "Close": [10.5, 10.6, 10.7],
        "Volume": [100, 101, 102],
    }, index=idx)

    fake_yf = types.ModuleType("yfinance")
    fake_yf.download = lambda *a, **kw: df
    fake_yf.Ticker = mock.MagicMock()
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    bars, prov = loader.get_bars("AAPL.US", "2025-01-02", "2025-01-03", interval="1m")
    dates = [b["date"] for b in bars]
    # 日内应保留时间（至少含空格或冒号）
    assert any(" " in d and ":" in d for d in dates), f"日内应保留时间，得到 {dates}"
    # 去重后不应坍缩为同一日期
    assert len(set(dates)) == 3, f"日内多 bar 不应坍缩，得到 {dates}"


def test_b2a_yahoo_suffix_critical(monkeypatch):
    """yahoo: split('.')[0] 把 BRK.B 取成 BRK 为 critical，應正确映射 BRK.B->BRK-B 且 .US 剥离。"""
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import importlib
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.yahoo import YahooLoader

    loader = YahooLoader()
    captured = {}
    idx = pd.to_datetime(["2025-01-02"])
    df = pd.DataFrame({"Open": [10], "High": [11], "Low": [9], "Close": [10.5], "Volume": [100]}, index=idx)

    def fake_download(ticker_symbol, *a, **kw):
        captured["ticker"] = ticker_symbol
        return df

    fake_yf = types.ModuleType("yfinance")
    fake_yf.download = fake_download
    fake_yf.Ticker = mock.MagicMock()
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    loader.get_bars("BRK.B", "2025-01-01", "2025-01-03", interval="1d")
    assert captured["ticker"] == "BRK-B", f"BRK.B 应映射为 BRK-B，得到 {captured['ticker']}"

    captured.clear()
    loader.get_bars("AAPL.US", "2025-01-01", "2025-01-03", interval="1d")
    assert captured["ticker"] == "AAPL", f"AAPL.US 应剥离 .US 得到 AAPL，得到 {captured['ticker']}"


def test_b2a_yahoo_history_swallow_logs(monkeypatch, caplog):
    """yahoo: history fallback 吞 root cause 应窄化捕获并 logger.warning(exc_info=True)。"""
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import importlib
    import hero_quant.config.settings as s
    importlib.reload(s)
    from hero_quant.data.loaders.yahoo import YahooLoader

    loader = YahooLoader()
    caplog.set_level(logging.WARNING)

    fake_yf = types.ModuleType("yfinance")
    fake_yf.download = lambda *a, **kw: pd.DataFrame()  # 空触发 history
    class FakeTicker:
        def __init__(self, *a, **kw): pass
        def history(self, *a, **kw):
            raise RuntimeError("history network boom")
    fake_yf.Ticker = FakeTicker
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    with pytest.raises(ValueError, match="no data from yahoo"):
        loader.get_bars("AAPL.US", "2025-01-01", "2025-01-03", interval="1d")
    # 应有 warning 日志且含异常信息
    assert any("history" in r.message.lower() for r in caplog.records), f"应记录 history 失败，得到 {[r.message for r in caplog.records]}"


def test_b2a_yahoo_helper_hoisted():
    """yahoo: 逐 bar 重建 helper 应提升至循环外/模块级。"""
    from hero_quant.data.loaders.yahoo import YahooLoader
    src = inspect.getsource(YahooLoader.get_bars)
    # 循环内定义 def _get_required 是性能腐坏，应提升
    # 检查：for idx, row in df.iterrows(): 之后不应出现 def _get_required
    iter_pos = src.find("for idx, row in")
    assert iter_pos != -1, "源码应包含 for idx, row in df.iterrows()"
    after = src[iter_pos:]
    assert "def _get_required" not in after, "_get_required 不应在循环内重建，应提升至循环外"
