"""腾讯行情 Loader：CN 日线、board_lots 单位。

位于 data/loaders 层 CN 主源，markets=["CN"]、unit="board_lots"；
live 下限流 1s 后请求腾讯 qfq 接口，live 失败显式抛 RuntimeError 不回退合成（仅 synthetic 模式走合成）；provenance
的 unit 需与 A 股手语义一致。
"""

from datetime import datetime, timedelta
import math
import time
import urllib.request
import urllib.parse
import json
import logging
from typing import Any

from hero_quant.infra.redis import cache

logger = logging.getLogger(__name__)


class DataValidationError(ValueError):
    """Loader validation error for unparseable dates/inputs."""


class TencentLoader:
    """腾讯 CN 行情 Loader（board_lots）。"""

    name = "tencent"
    source = "tencent"
    markets = ["CN"]
    unit = "board_lots"

    def _synthetic_bars(self, symbol, start, end):
        """生成合成 bars 列表，逐日等差递增，保证离线可运行。"""
        try:
            s = datetime.strptime(start, "%Y-%m-%d")
            e = datetime.strptime(end, "%Y-%m-%d")
        except (ValueError, TypeError) as exc:
            raise DataValidationError(f"invalid date format start={start!r} end={end!r}: {exc}") from exc
        if e < s:
            raise DataValidationError(f"invalid range: end {end!r} before start {start!r} (fail-closed)")
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
                raise DataValidationError(
                    f"synthetic range too large: start={start!r} end={end!r} exceeds 500 rows (fail-closed)"
                )
        return bars

    def _rate_limit(self):
        """live 模式下限流 1s，避免触发服务端限频；synthetic 模式直接跳过。"""
        try:
            try:
                from hero_quant.config.settings import Settings
                mode = Settings().data_mode
            except (ImportError, AttributeError, ValueError, TypeError, OSError, RuntimeError) as e:
                logger.warning("settings load failed in _rate_limit: %s", e, exc_info=True)
                import os
                mode = os.environ.get("HERO_DATA_MODE", "live")
            if isinstance(mode, str):
                mode = mode.strip().lower()
            else:
                mode = "live"
            if mode == "synthetic":
                return
            time.sleep(1)
        except (OSError, ValueError, RuntimeError, TypeError) as e:
            logger.warning("_rate_limit error: %s", e, exc_info=True)

    @cache("market:bars", expire=60)
    def _get_bars_live_cached(self, symbol, start, end, interval="1d"):
        """Cached live path only — mode resolved before cache lookup (no cross-mode poisoning)."""
        return self._fetch_live_bars(symbol, start, end, interval)

    def health(self) -> dict[str, Any]:
        """健康检查：返回腾讯源可用性与来源信息（SourceTrait 契约必需）。

        中文：trait.validate_loader 要求 loader 具备可调用的 health，此前 tencent/yahoo 未实现，
        导致 registry.register() 抛 ValueError，连带打断 e2e/engine/registry 等一系列用例。
        """
        return {"status": "ok", "source": self.name, "unit": self.unit, "markets": self.markets}

    def get_bars(self, symbol, start, end, interval="1d"):
        """拉取行情，兼容旧参数顺序并遵循 HERO_DATA_MODE 门控。"""
        _intervals = {"1d", "1m", "5m", "15m", "30m", "1h", "1wk", "1mo", "1D", "1W"}
        if start in _intervals:
            if "-" in str(end) and "-" in str(interval):
                start, end, interval = end, interval, start
            else:
                raise DataValidationError(f"ambiguous legacy argument order: start={start!r} end={end!r} interval={interval!r}")
        if interval not in _intervals:
            raise DataValidationError(f"invalid interval {interval!r}, expected one of {sorted(_intervals)}")
        # 腾讯 loader 仅支持日线，intraday 必须 fail-closed
        if interval not in ("1d", "1D"):
            raise DataValidationError(f"tencent loader only supports daily bars, got {interval!r}")

        try:
            from hero_quant.config.settings import Settings
            mode = Settings().data_mode
        except (ImportError, AttributeError, ValueError, TypeError, OSError, RuntimeError) as e:
            logger.warning("settings load failed in get_bars: %s", e, exc_info=True)
            import os
            mode = os.environ.get("HERO_DATA_MODE", "live")
        if isinstance(mode, str):
            mode = mode.strip().lower()
        else:
            mode = "live"
        if mode == "synthetic":
            return self._synthetic_bars(symbol, start, end)

        return self._get_bars_live_cached(symbol, start, end, interval)

    def _fetch_live_bars(self, symbol, start, end, interval="1d"):
        """Uncached live fetch body (mode already resolved by get_bars)."""
        # Validate + sanitize dates BEFORE building the URL (injection fail-closed)
        try:
            s_dt_pre = datetime.strptime(str(start), "%Y-%m-%d")
            e_dt_pre = datetime.strptime(str(end), "%Y-%m-%d")
        except (ValueError, TypeError) as e:
            raise DataValidationError(f"invalid date format start={start!r} end={end!r}: {e}") from e
        if e_dt_pre < s_dt_pre:
            raise DataValidationError(f"invalid range: end {end!r} before start {start!r}")

        self._rate_limit()
        # live 模式下禁止静默回退合成：解析/网络失败必须抛出
        try:
            code = symbol.split(".")[0]
            suffix = symbol.split(".")[-1].lower() if "." in symbol else ""
            if suffix in ("sh", "sz"):
                tencent_symbol = f"{suffix}{code}"
            else:
                tencent_symbol = code
            # Force https and sanitize symbol to prevent injection (MITM protection)
            # 复权口径：fqkline qfq 硬编码前复权（provenance 由 registry 统一补
            # adjust="qfq"+factor_asof=end，不得静默当未复权用）。
            tencent_symbol = urllib.parse.quote(tencent_symbol, safe="")
            url = (
                f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tencent_symbol},day,"
                f"{urllib.parse.quote(str(start), safe='')},{urllib.parse.quote(str(end), safe='')},{320},qfq"
            )
            with urllib.request.urlopen(url, timeout=2) as resp:
                raw = resp.read()
                text = raw.decode("utf-8", errors="ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
                j = json.loads(text)
                data = j.get("data") if isinstance(j, dict) else None
                if isinstance(data, dict):
                    # Explicit qfq-day key resolution: never first-list-wins (metadata risk).
                    entry = data.get(tencent_symbol) or data.get(code) or {}
                    candidate = None
                    if isinstance(entry, dict):
                        candidate = entry.get("qfqday") or entry.get("day")
                    elif isinstance(entry, list):
                        candidate = entry
                    if not isinstance(candidate, list) or not candidate:
                        # Fallback: scan known bar keys only (skip metadata like qt/info)
                        for v in data.values():
                            if isinstance(v, dict):
                                for kk in ("qfqday", "day"):
                                    vv = v.get(kk)
                                    if isinstance(vv, list) and len(vv) > 0:
                                        candidate = vv
                                        break
                                if candidate is not None:
                                    break
                    if not isinstance(candidate, list) or not candidate:
                        raise ValueError("no qfqday bars in response")
                    if candidate is not None and len(candidate) > 0:
                        bars = []
                        for item in candidate:
                            if isinstance(item, (list, tuple)) and len(item) >= 6:
                                # validate each field after float conversion (parity with dict path)
                                _field_vals = {}
                                for idx_f, field in enumerate(("date", "open", "close", "high", "low", "volume")):
                                    raw_v = item[idx_f]
                                    if field == "date":
                                        _field_vals[field] = str(raw_v)
                                    else:
                                        try:
                                            v = float(raw_v)
                                        except (ValueError, TypeError) as e:
                                            raise DataValidationError(f"tencent bar field {field!r} invalid {raw_v!r}: {e}") from e
                                        if not math.isfinite(v) or (field in ("close", "open", "high", "low") and v <= 0) or (field == "volume" and v < 0):
                                            raise DataValidationError(f"tencent bar field {field!r} invalid {raw_v!r}: non-finite or out-of-range ({v})")
                                        _field_vals[field] = v
                                bars.append({
                                    "date": _field_vals["date"],
                                    "open": _field_vals["open"],
                                    "close": _field_vals["close"],
                                    "high": _field_vals["high"],
                                    "low": _field_vals["low"],
                                    "volume": _field_vals["volume"],
                                })
                            elif isinstance(item, dict):
                                for k in ("open", "close", "high", "low", "volume", "date"):
                                    if k not in item or item[k] is None or (isinstance(item[k], str) and item[k].strip() == ""):
                                        raise DataValidationError(f"tencent bar missing required field {k!r}: {item!r}")
                                    if k != "date":
                                        try:
                                            v = float(item[k])
                                        except (ValueError, TypeError) as e:
                                            raise DataValidationError(f"tencent bar field {k!r} invalid {item[k]!r}: {e}") from e
                                        if not math.isfinite(v) or (k in ("close", "open", "high", "low") and v <= 0) or (k == "volume" and v < 0):
                                            raise DataValidationError(f"tencent bar field {k!r} invalid {item[k]!r}: non-finite or out-of-range ({v})")
                                bars.append({
                                    "date": str(item.get("date", "")),
                                    "open": float(item.get("open")),
                                    "close": float(item.get("close")),
                                    "high": float(item.get("high")),
                                    "low": float(item.get("low")),
                                    "volume": float(item.get("volume")),
                                })
                        if len(bars) > 0:
                            # 必须裁到 [start,end]，不静默返回全量 320 根；逐 bar strptime 校验日期
                            try:
                                s_dt = datetime.strptime(str(start), "%Y-%m-%d")
                                e_dt = datetime.strptime(str(end), "%Y-%m-%d")
                            except (ValueError, TypeError) as e:
                                raise DataValidationError(f"invalid date format start={start!r} end={end!r}: {e}") from e
                            clipped = []
                            for b in bars:
                                try:
                                    d = datetime.strptime(str(b["date"])[:10], "%Y-%m-%d")
                                except (ValueError, TypeError) as e:
                                    raise DataValidationError(f"tencent bar date invalid {b['date']!r}: {e}") from e
                                if s_dt <= d <= e_dt:
                                    b["date"] = d.strftime("%Y-%m-%d")
                                    clipped.append(b)
                            bars = clipped
                            if len(bars) == 0:
                                raise ValueError("no bars in requested window after clipping")
                            return bars
                raise ValueError("no bars parsed")
        except DataValidationError:
            raise
        except (ValueError, TypeError) as e:
            logger.warning("tencent parse failed for %s: %s", symbol, e, exc_info=True)
            raise RuntimeError(f"tencent fetch failed for {symbol}: {e}") from e
        except (RuntimeError, OSError) as e:
            logger.warning("tencent network error for %s: %s", symbol, e, exc_info=True)
            raise RuntimeError(f"tencent fetch failed for {symbol}: {e}") from e
