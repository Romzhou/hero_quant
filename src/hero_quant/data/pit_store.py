"""data.pit_store — PIT 日线 Parquet 存储（W1 · PR-122-a 小样本实现）。

设计
----
- **内容寻址**：`snapshot_id` 复用 `data.snapshot.snapshot_digest`（tenant/vendor/adjust/
  symbols/start/end/rows 的 sha256 前 20 位），同内容幂等、改一根 bar 必换 id；不另起 ID 规则。
- **分区**：`<root>/tenant=<t>/symbol=<s>/trade_date=<YYYY-MM-DD>/part-000.parquet`。
  symbol 含路径分隔符等非安全字符时按 RFC 3986 百分号编码后作目录名（清单里保留原始 symbol），
  避免 `BTC/USDT` 这类标的穿透目录层级。
- **快照隔离**：同一 `(tenant, symbol, trade_date)` 分区可能被多份快照命中，parquet 行内带
  `snapshot_id` 列；`as_of` 必按 snapshot_id 过滤，后写快照不得覆盖先写快照的行
  （否则 `get_snapshot` 的清单与 `as_of` 结果不再自洽，PIT 语义被静默破坏）。
- **清单**：`<root>/_index/<snapshot_id>.json`，临时文件 + `os.replace` 原子落盘。
- **根目录 fail-fast**：必须预先存在且可写，缺失/不可写立刻抛 `PitStoreUnavailable`
  （reason=`pit_root_missing` / `pit_root_not_writable`），**绝不 mkdir 兜底**——静默建目录
  会让「配错路径」看起来像成功。
- **as-of 同口径**：过滤 `trade_date <= as_of AND knowable_at <= as_of`，与 PG 版
  `SnapshotStore.as_of` 一致；DuckDB 只做 `read_parquet` 内存查询，不落持久 `.duckdb` 文件。
- **质量门**：写入前复用 `snapshot.normalize_bars` + `snapshot._enforce_quality`
  （后者为私有名，契约允许复用私有实现；见 PR-122-a 报告）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from hero_quant.config.settings import Settings
from hero_quant.data.snapshot import (
    SNAPSHOT_ID_HEX,
    SNAPSHOT_ID_PREFIX,
    SnapshotError,
    SnapshotNotFound,
    SnapshotUnavailable,
    SnapshotValidationError,
    _MAX_ADJUST_LEN,
    _MAX_LIMIT,
    _MAX_SYMBOL_LEN,
    _MAX_TENANT_LEN,
    _MAX_VENDOR_LEN,
    _as_date,
    _enforce_quality,
    _text,
    normalize_bars,
    snapshot_digest,
)

logger = logging.getLogger(__name__)

_BAR_COLUMNS = ("symbol", "trade_date", "knowable_at", "open", "high", "low", "close", "volume")
_BAR_SCHEMA = pa.schema(
    [
        pa.field("snapshot_id", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("trade_date", pa.date32(), nullable=False),
        pa.field("knowable_at", pa.date32(), nullable=False),
        pa.field("open", pa.float64(), nullable=False),
        pa.field("high", pa.float64(), nullable=False),
        pa.field("low", pa.float64(), nullable=False),
        pa.field("close", pa.float64(), nullable=False),
        pa.field("volume", pa.float64(), nullable=False),
    ]
)

_SAFE_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_SNAPSHOT_ID_LEN = len(SNAPSHOT_ID_PREFIX) + SNAPSHOT_ID_HEX
_SNAPSHOT_ID_RE = re.compile(rf"^{re.escape(SNAPSHOT_ID_PREFIX)}[0-9a-f]{{{SNAPSHOT_ID_HEX}}}$")


class PitStoreUnavailable(SnapshotUnavailable):
    """PIT 根目录不可用 —— 503；reason 为 `pit_root_missing` / `pit_root_not_writable`。

    继承 `SnapshotUnavailable`：既有按 503 捕获的调用方无需改动；
    另有运行期读写失败（`pit_root_unreadable`）沿用同一类型，避免新增对外异常体系。
    """

    error = "pit_store_unavailable"


def _required_text(value: Any, field: str, max_len: int) -> str:
    text = _text(value, field, max_len)
    if text is None:  # pragma: no cover - _text(required=True) 已先抛
        raise SnapshotValidationError(f"{field}_required")
    return text


def _tenant_component(value: Any) -> str:
    """tenant 直接作目录名，必须是安全 ASCII；非法值 fail-closed。"""
    tenant = _required_text(value, "tenant_id", _MAX_TENANT_LEN)
    if not _TENANT_RE.fullmatch(tenant):
        raise SnapshotValidationError("tenant_id_invalid")
    return tenant


def _symbol_text(value: Any) -> str:
    """symbol 的规范文本（strip 后）；`.`/`..` 一类目录穿越值直接拒绝。"""
    symbol = _required_text(value, "symbol", _MAX_SYMBOL_LEN)
    if symbol in (".", ".."):
        raise SnapshotValidationError("symbol_invalid")
    return symbol


def _symbol_component(value: Any) -> str:
    """symbol 作目录名：安全字符原样，其余百分号编码。"""
    symbol = _symbol_text(value)
    if _SAFE_SYMBOL_RE.fullmatch(symbol):
        return symbol
    return quote(symbol, safe="")


def _snapshot_id(value: Any) -> str:
    sid = _required_text(value, "snapshot_id", _MAX_SNAPSHOT_ID_LEN)
    if not _SNAPSHOT_ID_RE.fullmatch(sid):
        raise SnapshotValidationError("snapshot_id_invalid")
    return sid


def _is_writable(path: Path) -> bool:
    return os.access(path, os.W_OK | os.X_OK)


class PitParquetStore:
    """Parquet + DuckDB 的 PIT 日线存储；root 缺省取 `Settings().pit_root`。"""

    def __init__(self, root: str | Path | None = None) -> None:
        path = Path(root) if root is not None else Path(Settings().pit_root)
        if not path.exists():
            raise PitStoreUnavailable("pit_root_missing", str(path))
        if not path.is_dir():
            raise PitStoreUnavailable("pit_root_not_writable", "not_a_directory")
        if not _is_writable(path):
            raise PitStoreUnavailable("pit_root_not_writable", str(path))
        self.root = path

    # ------------------------------------------------------------------ 路径

    @property
    def _index_dir(self) -> Path:
        return self.root / "_index"

    def _symbol_dir(self, tenant: str, symbol: str) -> Path:
        return self.root / f"tenant={tenant}" / f"symbol={_symbol_component(symbol)}"

    def _partition_path(self, tenant: str, symbol: str, trade_date: date) -> Path:
        return self._symbol_dir(tenant, symbol) / f"trade_date={trade_date.isoformat()}" / "part-000.parquet"

    # ------------------------------------------------------------------ 写

    def write_daily(
        self,
        tenant_id: str,
        vendor: str,
        adjust: str,
        bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        fetched_at: datetime | None = None,
        knowable_at: date | None = None,
    ) -> dict:
        """落一份日线快照并返回清单（附 `created` 表示本次是否首次落盘）。

        幂等键是内容 sha256（与 PG 版同规则）：同内容重复写入返回首次清单且不改文件；
        `knowable_at` 以首次落盘为准。任何价格/日期/标的/复权变化都会产生新 snapshot_id。
        清单里的 `bytes` 是本次触碰的分区文件大小合计：分区被多份快照共用时含其它快照的行，
        磁盘真实占用请看 `dataset_stats()['bytes']`。
        """
        tenant = _tenant_component(tenant_id)
        vendor_name = _required_text(vendor, "vendor", _MAX_VENDOR_LEN)
        adjust_name = _required_text(adjust, "adjust", _MAX_ADJUST_LEN)
        if isinstance(bars_by_symbol, (str, bytes)) or not isinstance(bars_by_symbol, Mapping) or not bars_by_symbol:
            raise SnapshotValidationError("bars_empty")

        # normalize_bars 自行校验逐根字段/日期/OHLC/重复；窗口用 date.min..date.max（无窗口限制），
        # 真正的 start/end 由内容派生，保证同内容同 digest。
        rows = normalize_bars(bars_by_symbol, date.min, date.max)
        symbols = sorted({row["symbol"] for row in rows})
        for symbol in symbols:
            _symbol_component(symbol)
        start_date = rows[0]["trade_date"]
        end_date = rows[-1]["trade_date"]
        _enforce_quality(rows, symbols, start_date, end_date)

        snapshot_id, digest = snapshot_digest(
            tenant, vendor_name, adjust_name, symbols, start_date, end_date, rows
        )
        fetched = fetched_at or datetime.now(timezone.utc)
        if not isinstance(fetched, datetime):
            raise SnapshotValidationError("fetched_at_invalid")
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        knowable = _as_date(knowable_at, "knowable_at") if knowable_at is not None else None
        if knowable is not None and any(row["trade_date"] > knowable for row in rows):
            raise SnapshotValidationError("knowable_at_before_trade_date")

        manifest_path = self._index_dir / f"{snapshot_id}.json"
        existing = self._read_manifest(manifest_path, strict=True)
        if existing is not None:
            if existing.get("tenant_id") != tenant:  # pragma: no cover - 同 sid 跨租户不可能
                raise SnapshotError("pit_manifest_tenant_mismatch")
            return {**existing, "created": False}

        total_bytes = 0
        for row in rows:
            record = {
                "snapshot_id": snapshot_id,
                "symbol": row["symbol"],
                "trade_date": row["trade_date"],
                "knowable_at": knowable or row["trade_date"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
            }
            part = self._partition_path(tenant, row["symbol"], row["trade_date"])
            total_bytes += self._upsert_partition(part, record)

        manifest = {
            "snapshot_id": snapshot_id,
            "tenant_id": tenant,
            "vendor": vendor_name,
            "adjust": adjust_name,
            "symbols": symbols,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "fetched_at": fetched.isoformat(),
            "knowable_at": knowable.isoformat() if knowable is not None else None,
            "row_count": len(rows),
            "sha256": digest,
            "storage_uri": (self.root / f"tenant={tenant}").absolute().as_uri(),
            "rows": len(rows),
            "bytes": total_bytes,
            "partitions": len(rows),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write_manifest(manifest_path, manifest)
        return {**manifest, "created": True}

    def _upsert_partition(self, part: Path, record: dict) -> int:
        """把一行写入 `(tenant, symbol, trade_date)` 分区，保留其它快照的行（原子替换）。"""
        existing = self._read_part(part)
        kept = [
            item
            for item in existing
            if not (
                item["snapshot_id"] == record["snapshot_id"]
                and item["symbol"] == record["symbol"]
                and item["trade_date"] == record["trade_date"]
            )
        ]
        kept.append(record)
        kept.sort(key=lambda item: (item["snapshot_id"], item["symbol"], item["trade_date"]))
        table = pa.Table.from_pylist(kept, schema=_BAR_SCHEMA)
        tmp = part.with_name(f".{part.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
        try:
            part.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(table, tmp, compression="snappy")
            os.replace(tmp, part)
            size = part.stat().st_size
        except OSError as exc:
            raise PitStoreUnavailable("pit_root_not_writable", type(exc).__name__) from exc
        except pa.ArrowException as exc:
            raise SnapshotError("pit_write_failed", type(exc).__name__) from exc
        finally:
            tmp.unlink(missing_ok=True)
        return size

    def _read_part(self, part: Path) -> list[dict[str, Any]]:
        if not part.exists():
            return []
        try:
            table = pq.read_table(part)
        except (OSError, pa.ArrowException) as exc:
            raise SnapshotError("pit_part_unreadable", type(exc).__name__) from exc
        if not set(_BAR_COLUMNS) | {"snapshot_id"} <= set(table.column_names):
            raise SnapshotError("pit_part_schema_mismatch", part.name)
        return table.to_pylist()

    def _write_manifest(self, path: Path, manifest: dict[str, Any]) -> None:
        payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2)
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            raise PitStoreUnavailable("pit_root_not_writable", type(exc).__name__) from exc
        finally:
            tmp.unlink(missing_ok=True)

    # ------------------------------------------------------------------ 读

    def _read_manifest(self, path: Path, *, strict: bool) -> dict[str, Any] | None:
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            if strict:
                raise SnapshotError("pit_manifest_corrupt", type(exc).__name__) from exc
            logger.warning("skip corrupt PIT manifest %s: error_code=pit_manifest_corrupt", path.name)
            return None
        if not isinstance(data, dict) or not isinstance(data.get("snapshot_id"), str):
            if strict:
                raise SnapshotError("pit_manifest_corrupt", "manifest_shape_invalid")
            logger.warning("skip invalid PIT manifest %s: error_code=pit_manifest_corrupt", path.name)
            return None
        return data

    def get_snapshot(self, tenant_id: str, snapshot_id: str) -> dict:
        """按 snapshot_id 取清单；不存在或不属于该 tenant 一律 SnapshotNotFound。"""
        tenant = _tenant_component(tenant_id)
        sid = _snapshot_id(snapshot_id)
        manifest = self._read_manifest(self._index_dir / f"{sid}.json", strict=True)
        if manifest is None or manifest.get("tenant_id") != tenant:
            raise SnapshotNotFound("unknown snapshot")
        return {**manifest, "created": False}

    def as_of(
        self,
        tenant_id: str,
        snapshot_id: str,
        symbol: str,
        as_of: date | str,
        *,
        start: date | str | None = None,
        end: date | str | None = None,
        limit: int = 5000,
    ) -> list[dict]:
        """只返回该快照在 as_of 时点可见的日线（trade_date/knowable_at 双过滤）。

        返回行形状与 PG 版一致：`symbol, trade_date, knowable_at, open, high, low, close, volume`。
        """
        tenant = _tenant_component(tenant_id)
        sid = _snapshot_id(snapshot_id)
        symbol_name = _symbol_text(symbol)
        as_of_date = _as_date(as_of, "as_of")
        start_date = _as_date(start, "start") if start is not None else None
        end_date = _as_date(end, "end") if end is not None else None
        if start_date is not None and end_date is not None and start_date > end_date:
            raise SnapshotValidationError("start_after_end")

        # 先确认快照归属：不存在 / 跨租户与「空窗口」必须区分（也防跨租户探测）。
        manifest = self.get_snapshot(tenant, sid)
        if symbol_name not in (manifest.get("symbols") or []):
            return []

        files = sorted(
            str(path)
            for path in self._symbol_dir(tenant, symbol_name).glob("trade_date=*/part-000.parquet")
        )
        if not files:
            raise SnapshotError("pit_partition_missing", f"{sid}/{_symbol_component(symbol_name)}")

        sql = (
            "SELECT symbol, trade_date, knowable_at, open, high, low, close, volume "
            "FROM read_parquet(?) WHERE snapshot_id = ? AND trade_date <= ? AND knowable_at <= ?"
        )
        params: list[Any] = [files, sid, as_of_date, as_of_date]
        if start_date is not None:
            sql += " AND trade_date >= ?"
            params.append(start_date)
        if end_date is not None:
            sql += " AND trade_date <= ?"
            params.append(end_date)
        # 同一 (snapshot_id, symbol, trade_date) 至多一行：与 PG 版唯一约束同口径；
        # 并发重复写时保留更早的 knowable_at（首次落盘为准），避免同交易日出两行。
        sql += (
            " QUALIFY row_number() OVER (PARTITION BY symbol, trade_date ORDER BY knowable_at ASC) = 1"
            " ORDER BY trade_date ASC LIMIT ?"
        )
        params.append(max(1, min(int(limit), _MAX_LIMIT)))

        connection = duckdb.connect()
        try:
            result = connection.execute(sql, params).fetchall()
        except (duckdb.Error, OSError) as exc:
            raise SnapshotError("pit_query_failed", type(exc).__name__) from exc
        finally:
            connection.close()
        return [dict(zip(_BAR_COLUMNS, row)) for row in result]

    def list_snapshots(
        self,
        tenant_id: str,
        *,
        vendor: str | None = None,
        symbol: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        tenant = _tenant_component(tenant_id)
        vendor_name = _required_text(vendor, "vendor", _MAX_VENDOR_LEN) if vendor is not None else None
        symbol_name = _required_text(symbol, "symbol", _MAX_SYMBOL_LEN) if symbol is not None else None
        manifests = [
            item
            for item in self._iter_manifests()
            if item.get("tenant_id") == tenant
            and (vendor_name is None or item.get("vendor") == vendor_name)
            and (symbol_name is None or symbol_name in (item.get("symbols") or []))
        ]
        manifests.sort(key=lambda item: (str(item.get("fetched_at") or ""), str(item.get("snapshot_id"))), reverse=True)
        capped = max(1, min(int(limit), _MAX_LIMIT))
        window = manifests[max(0, int(offset)) : max(0, int(offset)) + capped]
        return [{**item, "created": False} for item in window]

    def dataset_stats(self, tenant_id: str | None = None) -> dict:
        """数据集统计：`rows` 按快照逻辑行数累加，`bytes/files/partitions` 为磁盘实际值。"""
        tenant = _tenant_component(tenant_id) if tenant_id is not None else None
        manifests = [item for item in self._iter_manifests() if tenant is None or item.get("tenant_id") == tenant]
        if tenant is None:
            pattern = "tenant=*/symbol=*/trade_date=*/part-000.parquet"
        else:
            pattern = f"tenant={tenant}/symbol=*/trade_date=*/part-000.parquet"
        files = sorted(self.root.glob(pattern))
        total_bytes = 0
        for path in files:
            try:
                total_bytes += path.stat().st_size
            except OSError as exc:
                raise PitStoreUnavailable("pit_root_unreadable", type(exc).__name__) from exc
        return {
            "root": str(self.root),
            "tenant_id": tenant,
            "snapshots": len(manifests),
            "rows": sum(int(item.get("row_count") or 0) for item in manifests),
            "bytes": total_bytes,
            "files": len(files),
            "partitions": len(files),
            "tenants": sorted({str(item.get("tenant_id")) for item in manifests if item.get("tenant_id")}),
            "symbols": sorted({str(sym) for item in manifests for sym in (item.get("symbols") or [])}),
            "vendors": sorted({str(item.get("vendor")) for item in manifests if item.get("vendor")}),
        }

    def _iter_manifests(self) -> list[dict[str, Any]]:
        if not self._index_dir.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(self._index_dir.glob("*.json")):
            manifest = self._read_manifest(path, strict=False)
            if manifest is not None:
                items.append(manifest)
        return items


__all__ = ["PitParquetStore", "PitStoreUnavailable"]
