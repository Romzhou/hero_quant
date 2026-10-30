"""data.snapshot — 数据快照清单与 PIT 日线（W3 PR-121/122 小样本实现）。

设计：
- **快照不可变**：`record_snapshot()` 对规范化后的 bars 做 sha256，`snapshot_id` 由
  内容派生；同租户同内容重复写入幂等，不产生第二份快照。
- **PIT 只认快照**：`as_of(tenant, snapshot_id, symbol, date)` 只读该快照，且
  `trade_date <= as_of AND knowable_at <= as_of`；日线默认 `knowable_at = trade_date`
  （收盘后可知），回填/修订可显式传更晚日期。
- **tenant 硬边界**：每条 SQL 带 `tenant_id = %s`，事务内 `set_config('app.tenant')`
  供 RLS FORCE 兜底；跨租户读不到（SnapshotNotFound）。
- **存储**：本切片用 PG 承载小样本（`pit_bars`）；Parquet/DuckDB 全量方案需要新增
  运行时依赖，待审批后替换存储层，公开接口（snapshot_id + as_of）保持不变。
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any

from hero_quant.data.quality import DataQualityError, assert_quality, check_bars
from hero_quant.db.pool import DatabaseUnavailable, get_pool
from hero_quant.db.rows import execute, executemany, fetch_all, fetch_one

try:  # 可选依赖：连接池超时映射为 503
    from psycopg_pool import PoolTimeout as _PoolTimeout  # type: ignore
except ImportError:  # pragma: no cover
    _PoolTimeout = ()  # type: ignore[assignment]

SNAPSHOT_ID_PREFIX = "snap_"
SNAPSHOT_ID_HEX = 20
_MAX_TENANT_LEN = 128
_MAX_SYMBOL_LEN = 64
_MAX_VENDOR_LEN = 64
_MAX_ADJUST_LEN = 32
_MAX_URI_LEN = 512
_MAX_LIMIT = 5000

_SNAPSHOT_COLUMNS = (
    "snapshot_id, tenant_id, vendor, symbols, start_date, end_date, adjust, fetched_at, "
    "row_count, sha256, storage_uri, meta"
)
_BAR_COLUMNS = "symbol, trade_date, knowable_at, open, high, low, close, volume"

_SNAPSHOT_INSERT_SQL = (
    "INSERT INTO data_snapshots "
    "(snapshot_id, tenant_id, vendor, symbols, start_date, end_date, adjust, fetched_at, "
    "row_count, sha256, storage_uri, meta) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
    f"ON CONFLICT (tenant_id, sha256) DO NOTHING RETURNING {_SNAPSHOT_COLUMNS}"
)
_BAR_INSERT_SQL = (
    "INSERT INTO pit_bars "
    "(tenant_id, snapshot_id, symbol, trade_date, knowable_at, open, high, low, close, volume) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (snapshot_id, symbol, trade_date) DO NOTHING"
)


class SnapshotError(RuntimeError):
    """快照存储错误基类。"""

    error = "snapshot_error"
    status_code = 500

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        message = f"{self.error}: {reason}"
        if detail:
            message += f" ({detail})"
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.error, "reason": self.reason}


class SnapshotUnavailable(SnapshotError):
    """无可用 PG —— 503。"""

    error = "snapshot_unavailable"
    status_code = 503


class SnapshotNotFound(SnapshotError):
    """快照不存在或不属于该 tenant —— 404。"""

    error = "snapshot_not_found"
    status_code = 404


class SnapshotValidationError(SnapshotError):
    """入参/行情数据非法 —— 400。"""

    error = "snapshot_invalid"
    status_code = 400


class SnapshotQualityError(SnapshotValidationError):
    """数据质量门阻断 —— 400；`reason` 是 quality.py 的稳定机器码。

    继承 SnapshotValidationError：既有调用方/测试按原类型捕获即可，行为不变。
    """

    error = "snapshot_invalid"
    status_code = 400

    def __init__(self, reason: str, detail: str = "", codes: Sequence[str] = ()) -> None:
        self.detail = detail
        self.codes = tuple(codes)
        super().__init__(reason, detail)


def _text(value: Any, field: str, max_len: int, *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise SnapshotValidationError(f"{field}_required")
        return None
    if not isinstance(value, str):
        raise SnapshotValidationError(f"{field}_must_be_str")
    text = value.strip()
    if not text:
        if required:
            raise SnapshotValidationError(f"{field}_required")
        return None
    if len(text) > max_len:
        raise SnapshotValidationError(f"{field}_too_long")
    return text


def _as_date(value: Any, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError as exc:
            raise SnapshotValidationError(f"{field}_invalid_date") from exc
    raise SnapshotValidationError(f"{field}_invalid_date")


def _finite(value: Any, field: str, *, positive: bool) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError(f"{field}_not_numeric") from exc
    if not math.isfinite(number):
        raise SnapshotValidationError(f"{field}_not_finite")
    if positive and number <= 0:
        raise SnapshotValidationError(f"{field}_must_be_positive")
    if not positive and number < 0:
        raise SnapshotValidationError(f"{field}_must_be_non_negative")
    return number


def normalize_bars(
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    start: date | datetime | str,
    end: date | datetime | str,
) -> list[dict[str, Any]]:
    """校验并展平 bars，返回按 (symbol, trade_date) 排序的规范行。

    规则（fail-closed）：symbol 非空；每根 bar 的 OHLC 有限且 >0、volume ≥0；
    high/low 与 open/close 关系一致；日期落在 [start, end]；同 symbol 同日期不得重复。
    """
    start_date = _as_date(start, "start")
    end_date = _as_date(end, "end")
    if start_date > end_date:
        raise SnapshotValidationError("start_after_end")
    if not isinstance(bars_by_symbol, Mapping) or not bars_by_symbol:
        raise SnapshotValidationError("bars_empty")
    keys = list(bars_by_symbol.keys())
    if not all(isinstance(key, str) for key in keys):
        raise SnapshotValidationError("symbol_must_be_str")

    rows: list[dict[str, Any]] = []
    for raw_symbol in sorted(keys):
        symbol = _text(raw_symbol, "symbol", _MAX_SYMBOL_LEN)
        bars = bars_by_symbol[raw_symbol]
        if not isinstance(bars, Mapping) and hasattr(bars, "to_dict"):
            # DataFrame → records（pandas 是数据层既有依赖；index 可能是 date/时间列）
            try:
                frame = bars.reset_index() if hasattr(bars, "reset_index") else bars
                bars = frame.to_dict("records")
            except (TypeError, ValueError) as exc:
                raise SnapshotValidationError(f"bars_unconvertible:{symbol}") from exc
        if isinstance(bars, (str, bytes)) or not isinstance(bars, Sequence) or not bars:
            raise SnapshotValidationError(f"bars_empty:{symbol}")
        seen: set[date] = set()
        for bar in bars:
            if not isinstance(bar, Mapping):
                raise SnapshotValidationError(f"bar_not_mapping:{symbol}")
            raw_date = None
            for date_key in ("date", "trade_date", "index"):
                if bar.get(date_key) is not None:
                    raw_date = bar.get(date_key)
                    break
            trade_date = _as_date(raw_date, f"{symbol}.date")
            if trade_date < start_date or trade_date > end_date:
                raise SnapshotValidationError(f"bar_out_of_window:{symbol}:{trade_date.isoformat()}")
            if trade_date in seen:
                raise SnapshotValidationError(f"duplicate_bar:{symbol}:{trade_date.isoformat()}")
            seen.add(trade_date)
            open_p = _finite(bar.get("open"), f"{symbol}.open", positive=True)
            high_p = _finite(bar.get("high"), f"{symbol}.high", positive=True)
            low_p = _finite(bar.get("low"), f"{symbol}.low", positive=True)
            close_p = _finite(bar.get("close"), f"{symbol}.close", positive=True)
            volume = _finite(bar.get("volume"), f"{symbol}.volume", positive=False)
            if high_p < low_p or high_p < max(open_p, close_p) or low_p > min(open_p, close_p):
                raise SnapshotValidationError(f"ohlc_inconsistent:{symbol}:{trade_date.isoformat()}")
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "open": open_p,
                    "high": high_p,
                    "low": low_p,
                    "close": close_p,
                    "volume": volume,
                }
            )
    rows.sort(key=lambda item: (item["symbol"], item["trade_date"]))
    return rows


def _enforce_quality(
    rows: Sequence[Mapping[str, Any]],
    symbols: Sequence[str],
    start: date,
    end: date,
) -> None:
    """质量门：在 digest/入库之前阻断坏数据，坏数据不产生 snapshot_id。

    逐根字段级校验已由 `normalize_bars` 完成；这里只跑它覆盖不到的时序/跨源层面
    （`data.quality`）。完整性有两条路：横截面覆盖率（标的间互相比，不需要日历，
    本路径始终生效）+ 区间缺口/首尾（需显式 calendar；仓库没有交易日历源，用工作日
    估算会把长假整段判成缺口，是假阳性，所以不在本处开启）。
    """
    report = check_bars(rows, symbols=symbols, start=start, end=end)
    try:
        assert_quality(report)
    except DataQualityError as exc:
        raise SnapshotQualityError(exc.reason, exc.detail, exc.codes) from exc


def snapshot_digest(
    tenant_id: str,
    vendor: str,
    adjust: str,
    symbols: Sequence[str],
    start: date,
    end: date,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[str, str]:
    """返回 (snapshot_id, sha256)；同租户同内容稳定，跨内容必变。"""
    payload = {
        "tenant_id": tenant_id,
        "vendor": vendor,
        "adjust": adjust,
        "symbols": sorted(symbols),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rows": [
            {
                "symbol": row["symbol"],
                "trade_date": row["trade_date"].isoformat(),
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
            }
            for row in rows
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"{SNAPSHOT_ID_PREFIX}{digest[:SNAPSHOT_ID_HEX]}", digest


class SnapshotStore:
    """PG 快照/PIT 存储；构造需显式连接池。"""

    def __init__(self, pool: Any) -> None:
        if pool is None or not callable(getattr(pool, "connection", None)):
            raise ValueError("pool must provide a callable .connection()")
        self._pool = pool

    @classmethod
    def from_dsn(cls, dsn: str | None = None) -> "SnapshotStore":
        try:
            return cls(get_pool(dsn))
        except DatabaseUnavailable as exc:
            raise SnapshotUnavailable(exc.reason, str(exc)) from exc

    @contextmanager
    def _tx(self, tenant_id: str) -> Iterator[Any]:
        tenant = _text(tenant_id, "tenant_id", _MAX_TENANT_LEN)
        try:
            ctx = self._pool.connection()
        except Exception as exc:  # noqa: BLE001 - 取连接失败一律 503
            raise SnapshotUnavailable("pool_connection_failed", type(exc).__name__) from exc
        try:
            with ctx as conn:
                with conn.transaction():
                    execute(conn, "SELECT set_config('app.tenant', %s, true)", (tenant,))
                    execute(conn, "SELECT set_config('app.current_tenant', %s, true)", (tenant,))
                    yield conn
        except _PoolTimeout as exc:  # type: ignore[misc]
            raise SnapshotUnavailable("pool_timeout", type(exc).__name__) from exc

    def record_snapshot(
        self,
        tenant_id: str,
        vendor: str,
        symbols: Sequence[str],
        start: date | datetime | str,
        end: date | datetime | str,
        bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        adjust: str = "unknown",
        fetched_at: datetime | None = None,
        knowable_at: date | datetime | str | None = None,
        storage_uri: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """写入一份快照（幂等）；返回清单行，附 `created` 表示是否首次落库。

        幂等键是 (tenant_id, 内容 sha256)：同内容重复写入返回首次行，`knowable_at`
        也以首次落库为准；内容任何变化（价格/日期/标的/复权口径）都会产生新 snapshot_id。
        """
        tenant = _text(tenant_id, "tenant_id", _MAX_TENANT_LEN)
        vendor_name = _text(vendor, "vendor", _MAX_VENDOR_LEN)
        adjust_name = _text(adjust, "adjust", _MAX_ADJUST_LEN) or "unknown"
        start_date = _as_date(start, "start")
        end_date = _as_date(end, "end")

        if isinstance(symbols, (str, bytes)) or not isinstance(symbols, Sequence) or not symbols:
            raise SnapshotValidationError("symbols_empty_or_duplicate")
        symbol_list = [_text(item, "symbol", _MAX_SYMBOL_LEN) for item in symbols]
        if len(set(symbol_list)) != len(symbol_list):
            raise SnapshotValidationError("symbols_empty_or_duplicate")
        if set(symbol_list) != set(bars_by_symbol.keys()):
            raise SnapshotValidationError("symbols_mismatch_bars")

        rows = normalize_bars(bars_by_symbol, start_date, end_date)
        _enforce_quality(rows, symbol_list, start_date, end_date)
        snapshot_id, digest = snapshot_digest(
            tenant, vendor_name, adjust_name or "unknown", symbol_list, start_date, end_date, rows
        )

        fetched = fetched_at or datetime.now(timezone.utc)
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        knowable = _as_date(knowable_at, "knowable_at") if knowable_at is not None else None
        if knowable is not None and any(row["trade_date"] > knowable for row in rows):
            raise SnapshotValidationError("knowable_at_before_trade_date")
        uri = _text(storage_uri, "storage_uri", _MAX_URI_LEN, required=False) or (
            f"pg://pit_bars/{snapshot_id}"
        )
        if meta is not None and not isinstance(meta, Mapping):
            raise SnapshotValidationError("meta_must_be_mapping")
        meta_json = json.dumps(dict(meta or {}), ensure_ascii=False, default=str)

        with self._tx(tenant) as conn:
            row = fetch_one(
                conn,
                _SNAPSHOT_INSERT_SQL,
                (
                    snapshot_id,
                    tenant,
                    vendor_name,
                    symbol_list,
                    start_date,
                    end_date,
                    adjust_name,
                    fetched,
                    len(rows),
                    digest,
                    uri,
                    meta_json,
                ),
            )
            if row is None:
                existing = fetch_one(
                    conn,
                    f"SELECT {_SNAPSHOT_COLUMNS} FROM data_snapshots "
                    "WHERE tenant_id = %s AND sha256 = %s",
                    (tenant, digest),
                )
                if existing is None:
                    raise SnapshotError("snapshot_insert_conflict_without_existing_row")
                return {**existing, "created": False}

            executemany(
                conn,
                _BAR_INSERT_SQL,
                [
                    (
                        tenant,
                        snapshot_id,
                        item["symbol"],
                        item["trade_date"],
                        knowable or item["trade_date"],
                        item["open"],
                        item["high"],
                        item["low"],
                        item["close"],
                        item["volume"],
                    )
                    for item in rows
                ],
            )
        return {**row, "created": True}

    def get_snapshot(self, tenant_id: str, snapshot_id: str) -> dict[str, Any]:
        tenant = _text(tenant_id, "tenant_id", _MAX_TENANT_LEN)
        sid = _text(snapshot_id, "snapshot_id", _MAX_TENANT_LEN)
        with self._tx(tenant) as conn:
            row = fetch_one(
                conn,
                f"SELECT {_SNAPSHOT_COLUMNS} FROM data_snapshots "
                "WHERE tenant_id = %s AND snapshot_id = %s",
                (tenant, sid),
            )
        if row is None:
            raise SnapshotNotFound("unknown snapshot")
        return row

    def list_snapshots(
        self,
        tenant_id: str,
        *,
        vendor: str | None = None,
        symbol: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        tenant = _text(tenant_id, "tenant_id", _MAX_TENANT_LEN)
        clauses = ["tenant_id = %s"]
        params: list[Any] = [tenant]
        if vendor is not None:
            clauses.append("vendor = %s")
            params.append(_text(vendor, "vendor", _MAX_VENDOR_LEN))
        if symbol is not None:
            clauses.append("%s = ANY(symbols)")
            params.append(_text(symbol, "symbol", _MAX_SYMBOL_LEN))
        capped = max(1, min(int(limit), _MAX_LIMIT))
        params.extend([capped, max(0, int(offset))])
        with self._tx(tenant) as conn:
            return fetch_all(
                conn,
                f"SELECT {_SNAPSHOT_COLUMNS} FROM data_snapshots WHERE {' AND '.join(clauses)} "
                "ORDER BY fetched_at DESC, snapshot_id DESC LIMIT %s OFFSET %s",
                tuple(params),
            )

    def as_of(
        self,
        tenant_id: str,
        snapshot_id: str,
        symbol: str,
        as_of: date | datetime | str,
        *,
        start: date | datetime | str | None = None,
        end: date | datetime | str | None = None,
        limit: int = _MAX_LIMIT,
    ) -> list[dict[str, Any]]:
        """只返回该快照在 as_of 时点可见的日线（trade_date/knowable_at 双过滤）。"""
        tenant = _text(tenant_id, "tenant_id", _MAX_TENANT_LEN)
        sid = _text(snapshot_id, "snapshot_id", _MAX_TENANT_LEN)
        sym = _text(symbol, "symbol", _MAX_SYMBOL_LEN)
        as_of_date = _as_date(as_of, "as_of")
        start_date = _as_date(start, "start") if start is not None else None
        end_date = _as_date(end, "end") if end is not None else None
        if start_date is not None and end_date is not None and start_date > end_date:
            raise SnapshotValidationError("start_after_end")

        # 先确认快照归属，避免把「不存在」和「空窗口」混为一谈（也防跨租户探测）。
        self.get_snapshot(tenant, sid)

        clauses = [
            "tenant_id = %s",
            "snapshot_id = %s",
            "symbol = %s",
            "trade_date <= %s",
            "knowable_at <= %s",
        ]
        params: list[Any] = [tenant, sid, sym, as_of_date, as_of_date]
        if start_date is not None:
            clauses.append("trade_date >= %s")
            params.append(start_date)
        if end_date is not None:
            clauses.append("trade_date <= %s")
            params.append(end_date)
        capped = max(1, min(int(limit), _MAX_LIMIT))
        params.append(capped)
        with self._tx(tenant) as conn:
            return fetch_all(
                conn,
                f"SELECT {_BAR_COLUMNS} FROM pit_bars WHERE {' AND '.join(clauses)} "
                "ORDER BY trade_date ASC LIMIT %s",
                tuple(params),
            )


def capture_snapshot(
    store: SnapshotStore,
    tenant_id: str,
    registry: Any,
    symbols: Sequence[str],
    start: date | datetime | str,
    end: date | datetime | str,
    *,
    adjust: str = "unknown",
    **snapshot_kwargs: Any,
) -> dict[str, Any]:
    """从行情注册表取数并落一份快照：每次取数都有 snapshot_id + sha256。

    只依赖 registry 的 `get_bars(symbol, start, end)` 契约（返回 bars 或
    `(bars, provenance)`）；provenance 的 source/unit/adjust 写入 meta，vendor 取
    所有标的的共同 source，混合来源显式记 "mixed"。
    """
    if isinstance(symbols, (str, bytes)) or not isinstance(symbols, Sequence) or not symbols:
        raise SnapshotValidationError("symbols_empty_or_duplicate")
    symbol_list = [_text(item, "symbol", _MAX_SYMBOL_LEN) for item in symbols]
    if len(set(symbol_list)) != len(symbol_list):
        # 必须在取数前拒绝：dict 收集会把重复 symbol 静默合并，
        # 到 record_snapshot 再校验就永远触发不了（P0：重复取数被当成一份）。
        raise SnapshotValidationError("symbols_empty_or_duplicate")
    bars_by_symbol: dict[str, Any] = {}
    provenance: dict[str, dict[str, Any]] = {}
    for symbol in symbol_list:
        result = registry.get_bars(symbol, start, end)
        if isinstance(result, tuple) and len(result) == 2:
            bars, prov = result
        else:
            bars, prov = result, None
        bars_by_symbol[symbol] = bars
        if prov is not None:
            provenance[symbol] = {
                "source": getattr(prov, "source", None),
                "unit": getattr(prov, "unit", None),
                "adjust": getattr(prov, "adjust", None),
            }

    sources = {item["source"] for item in provenance.values() if item.get("source")}
    adjustments = {
        item["adjust"]
        for item in provenance.values()
        if item.get("adjust") not in (None, "", "unknown")
    }
    vendor = next(iter(sources)) if len(sources) == 1 else ("mixed" if sources else "unknown")
    effective_adjust = (
        adjust if adjust != "unknown" else (next(iter(adjustments)) if len(adjustments) == 1 else "unknown")
    )

    meta = dict(snapshot_kwargs.pop("meta", {}) or {})
    meta["provenance"] = provenance
    return store.record_snapshot(
        tenant_id,
        vendor,
        symbol_list,
        start,
        end,
        bars_by_symbol,
        adjust=effective_adjust,
        meta=meta,
        **snapshot_kwargs,
    )


_store: SnapshotStore | None = None
_store_lock = threading.Lock()


def get_snapshot_store() -> SnapshotStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = SnapshotStore.from_dsn()
        return _store


def reset_snapshot_store() -> None:
    global _store
    with _store_lock:
        _store = None


__all__ = [
    "SNAPSHOT_ID_PREFIX",
    "SnapshotError",
    "SnapshotNotFound",
    "SnapshotQualityError",
    "SnapshotStore",
    "SnapshotUnavailable",
    "SnapshotValidationError",
    "capture_snapshot",
    "get_snapshot_store",
    "normalize_bars",
    "reset_snapshot_store",
    "snapshot_digest",
]
