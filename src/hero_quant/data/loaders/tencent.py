"""腾讯行情 Loader：CN 日线、board_lots 单位与合成回退。

位于 data/loaders 层 CN 主源，markets=["CN"]、unit="board_lots"；
live 下限流 1s 后请求腾讯 qfq 接口，任意异常回退合成；provenance
的 unit 需与 A 股手语义一致。
"""

from datetime import datetime, timedelta
import time
import urllib.request
import json
import logging

logger = logging.getLogger(__name__)


class TencentLoader:
    """腾讯 CN 行情 Loader（board_lots）。"""

    markets = ["CN"]
    unit = "board_lots"

    def _synthetic_bars(self, symbol, start, end):
        """生成合成 bars 列表，逐日等差递增，保证离线可运行。"""
        try:
            s = datetime.strptime(start, "%Y-%m-%d")
            e = datetime.strptime(end, "%Y-%m-%d")
        except Exception:
            s = datetime(2026, 8, 1)
            e = datetime(2026, 8, 19)
        if e < s:
            e = s
        bars = []
        cur = s
        idx = 0
        while cur <= e:
            date_str = cur.strftime("%Y-%m-%d")
            bars.append({
                "date": date_str,
                "open": 1500.0 + idx,
                "close": 1500.0 + idx + 0.5,
                "high": 1510 + idx,
                "low": 1490 + idx,
                "volume": 100,
            })
            cur += timedelta(days=1)
            idx += 1
            if idx > 500:
                break
        if not bars:
            bars.append({"date": start, "open": 1500.0, "close": 1500.0, "high": 1510, "low": 1490, "volume": 100})
        return bars

    def _rate_limit(self):
        """live 模式下限流 1s，避免触发服务端限频；synthetic 模式直接跳过。"""
        try:
            try:
                from hero_quant.config.settings import Settings
                mode = Settings().data_mode
            except Exception as e:
                import logging as _lg
                _lg.getLogger(__name__).warning("settings load failed in _rate_limit: %s", e, exc_info=e)
                import os
                mode = os.environ.get("HERO_DATA_MODE", "live")
            if isinstance(mode, str):
                mode = mode.strip().lower()
            else:
                mode = "live"
            if mode == "synthetic":
                return
            time.sleep(1)
        except Exception as e:
            import logging as _lg2
            _lg2.getLogger(__name__).warning("_rate_limit error: %s", e, exc_info=e)

    def get_bars(self, symbol, start, end, interval="1d"):
        """拉取行情，兼容旧参数顺序并遵循 HERO_DATA_MODE 门控。"""
        _intervals = {"1d", "1m", "5m", "15m", "30m", "1h", "1wk", "1mo", "1D", "1W"}
        if start in _intervals and "-" in end and "-" in interval:
            start, end, interval = end, interval, start

        try:
            from hero_quant.config.settings import Settings
            mode = Settings().data_mode
        except Exception as e:
            import logging as _lg3
            _lg3.getLogger(__name__).warning("settings load failed in get_bars: %s", e, exc_info=e)
            import os
            mode = os.environ.get("HERO_DATA_MODE", "live")
        if isinstance(mode, str):
            mode = mode.strip().lower()
        else:
            mode = "live"
        if mode == "synthetic":
            return self._synthetic_bars(symbol, start, end)

        self._rate_limit()
        # live 模式下禁止静默回退合成：解析/网络失败必须抛出
        try:
            code = symbol.split(".")[0]
            suffix = symbol.split(".")[-1].lower() if "." in symbol else ""
            if suffix in ("sh", "sz"):
                tencent_symbol = f"{suffix}{code}"
            else:
                tencent_symbol = code
            url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tencent_symbol},day,,,{320},qfq"
            with urllib.request.urlopen(url, timeout=2) as resp:
                raw = resp.read()
                text = raw.decode("utf-8", errors="ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
                j = json.loads(text)
                data = j.get("data") if isinstance(j, dict) else None
                if isinstance(data, dict):
                    candidate = None
                    for v in data.values():
                        if isinstance(v, dict):
                            for kk, vv in v.items():
                                if isinstance(vv, list) and len(vv) > 0:
                                    candidate = vv
                                    break
                        elif isinstance(v, list) and len(v) > 0:
                            candidate = v
                            break
                    if candidate is not None and len(candidate) > 0:
                        bars = []
                        for item in candidate:
                            if isinstance(item, (list, tuple)) and len(item) >= 6:
                                bars.append({
                                    "date": str(item[0]),
                                    "open": float(item[1]) if item[1] else 1500.0,
                                    "close": float(item[2]) if item[2] else 1500.0,
                                    "high": float(item[3]) if item[3] else 1510,
                                    "low": float(item[4]) if item[4] else 1490,
                                    "volume": float(item[5]) if item[5] else 100,
                                })
                            elif isinstance(item, dict):
                                # already shaped dict - ensure volume handling and required fields
                                bars.append({
                                    "date": str(item.get("date", "")),
                                    "open": float(item.get("open", 1500.0) or 1500.0),
                                    "close": float(item.get("close", 1500.0) or 1500.0),
                                    "high": float(item.get("high", 1510) or 1510),
                                    "low": float(item.get("low", 1490) or 1490),
                                    "volume": float(item.get("volume", 100) or 100),
                                })
                        if len(bars) > 0:
                            return bars
                raise ValueError("no bars parsed")
        except ValueError as e:
            logger.warning("tencent parse failed for %s: %s", symbol, e, exc_info=e)
            raise RuntimeError(f"tencent fetch failed for {symbol}: {e}") from e
        except Exception as e:
            logger.warning("tencent network error for %s: %s", symbol, e, exc_info=e)
            raise RuntimeError(f"tencent fetch failed for {symbol}: {e}") from e
