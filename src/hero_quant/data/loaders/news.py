"""PIT 新闻快照加载器：按 trade_date 过滤 + PIT 诚实标注。

职责：离线/合成新闻记录的 PIT 过滤，不联网，不伪造 PIT。
核心规则：只有存在可验证发布时间且发布时间 ≤ 快照时间才 pit=True，否则 pit=False；
缺失时间戳时 pit_status 为 unknown/unavailable，绝不伪装为 PIT。

T4-4 收口：新闻 PIT 需要 available_at（数据可得时间）防未来函数。
- 全局快照别名优先级统一为 snapshot_date > available_at > snapshot
  （kwargs 兜底 snapshot_time/avail_at/pit_snapshot 排最后）。
- 全局快照（available_at 族）是 PIT 必填：缺失时 fail-closed 明确标记
  pit=False + usable_for_live=False + provenance.available_at，不当 live/回测用；
  合成/历史离线使用必须显式 allow_synthetic=True 或 historical_mode=True。
- 记录级 available_at（同优先级）若存在则必须 ≤ 全局快照，否则按 future
  判非 PIT；缺记录级 available_at 时为兼容存量仍可按 publish_time 判 pit，
  但 usable_for_live=False（缺 available_at 不被当 live 用）。
- 时间解析：全局快照与记录级 available_at 非法时抛 ValueError（不静默）；
  publish_time / trade_date 保持历史宽松语义（诚实标 unknown，不抛错），
  以兼容存量测试与诚实标注约定。
"""

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "load_news",
    "get_disclosure",
    "build_disclosure",
    "format_disclosure",
    "get_pit_disclosure",
    "build_news_disclosure",
    "news_disclosure",
]

# 候选发布时间字段（按优先级）；移除 `date` 避免与交易日标签混淆导致伪造 PIT
_PUBLISH_KEYS = (
    "publish_time",
    "published_at",
    "published_time",
    "publish_date",
    "published",
    "timestamp",
    "datetime",
    "time",
)


def _parse_time(value) -> pd.Timestamp | None:
    """宽松解析时间为 Timestamp，失败返回 None。窄化异常捕获。"""
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        return value
    try:
        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return None
        return ts
    except (ValueError, TypeError, pd.errors.OutOfBoundsDatetime):
        logger.debug("news _parse_time unparseable %r", value)
        return None


def _parse_time_strict(value, *, field: str = "available_at") -> pd.Timestamp:
    """严格解析 available_at 族时间：非法/缺失时抛 ValueError，不静默。

    仅用于全局快照与记录级 available_at（PIT 必填字段）。publish_time /
    trade_date 保持宽松语义（见 _parse_time），以兼容存量诚实标注测试。
    """
    if value is None:
        raise ValueError(f"news {field} is required for PIT (fail-closed): got None")
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"news {field} is required for PIT (fail-closed): got empty string")
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            raise ValueError(f"news {field} is required for PIT (fail-closed): got NaT")
        return value
    try:
        ts = pd.to_datetime(value, errors="raise")
    except (ValueError, TypeError, pd.errors.OutOfBoundsDatetime) as e:
        raise ValueError(f"news {field} unparseable {value!r}: {e}") from e
    if pd.isna(ts):
        raise ValueError(f"news {field} unparseable {value!r}: got NaT")
    return ts


def _pick_snapshot_raw(
    snapshot_date=None,
    available_at=None,
    snapshot=None,
    kwargs: dict | None = None,
) -> tuple[object, str | None]:
    """统一快照别名优先级：snapshot_date > available_at > snapshot > kwargs 兜底。

    返回 (raw_value, source_name)。source_name 仅用于 provenance/报错。
    空串/空白视为缺失并落到下一优先级；非空非法值由严格解析抛错。
    """

    def _missing(v) -> bool:
        return v is None or (isinstance(v, str) and not v.strip())

    if not _missing(snapshot_date):
        return snapshot_date, "snapshot_date"
    if not _missing(available_at):
        return available_at, "available_at"
    if not _missing(snapshot):
        return snapshot, "snapshot"
    if kwargs:
        for alias in ("snapshot_time", "avail_at", "pit_snapshot"):
            if alias in kwargs and not _missing(kwargs[alias]):
                return kwargs[alias], alias
    return None, None


def _extract_publish_time(record: dict) -> pd.Timestamp | None:
    for k in _PUBLISH_KEYS:
        if k not in record or record[k] is None:
            continue
        v = record[k]
        if isinstance(v, str) and not v.strip():
            continue
        ts = _parse_time(v)
        if ts is not None:
            return ts
    return None


def _extract_available_at(record: dict) -> tuple[pd.Timestamp | None, str | None, bool]:
    """提取记录级 available_at（数据可得时间），同全局快照优先级。

    优先级：snapshot_date > available_at > snapshot。
    返回 (ts_or_None, source_name_or_None, malformed_bool)。
    malformed=True 表示字段存在但非法（调用方应整体抛 ValueError，不静默）。
    缺失（字段均不存在/None/空串）返回 (None, None, False)。
    """
    for key in ("snapshot_date", "available_at", "snapshot"):
        if key not in record:
            continue
        v = record[key]
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        try:
            return _parse_time_strict(v, field=f"record {key}"), key, False
        except ValueError:
            return None, key, True
    return None, None, False


def _normalize_date_str(value) -> str | None:
    """归一化为 YYYY-MM-DD 字符串用于 trade_date 过滤。"""
    if value is None:
        return None
    ts = _parse_time(value)
    if ts is None:
        return None
    try:
        return ts.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        logger.warning("news _normalize_date_str strftime failed for %r", value, exc_info=True)
        return str(value).strip()[:10]


def _extract_trade_date(record: dict) -> str | None:
    for k in ("trade_date", "trading_date", "tradeDate"):
        if k in record and record[k] is not None:
            v = _normalize_date_str(record[k])
            if v:
                return v
    return None


def _is_aware(ts: pd.Timestamp) -> bool:
    """模块级 helper：判断 Timestamp 是否为带时区；避免逐行重建函数对象。"""
    try:
        tz = getattr(ts, "tz", None)
        if tz is not None:
            return True
    except (AttributeError, TypeError, ValueError):
        logger.debug("news _is_aware tz check failed for %r", ts, exc_info=True)
    return getattr(ts, "tzinfo", None) is not None


def _resolve_snapshot_for_record(
    record: dict,
    global_snapshot: pd.Timestamp | None,
) -> pd.Timestamp | None:
    """仅使用全局快照；记录级 snapshot 忽略以避免伪造 PIT。

    记录级 available_at 不参与快照比较（全局快照唯一 governs），仅用于
    usable_for_live 门控与 provenance 标注（见 load_news）。
    """
    return global_snapshot


def _tz_compare_le(a: pd.Timestamp, b: pd.Timestamp) -> bool:
    """naive/aware 归一后比较 a <= b（naive 按 UTC 理解）。"""
    pub_aware = _is_aware(a)
    snap_aware = _is_aware(b)
    if pub_aware != snap_aware:
        if not pub_aware and snap_aware:
            a = a.tz_localize("UTC")
        elif pub_aware and not snap_aware:
            b = b.tz_localize("UTC")
    if _is_aware(a) and _is_aware(b):
        try:
            return bool(a.tz_convert("UTC") <= b.tz_convert("UTC"))
        except (TypeError, ValueError, AttributeError) as e:
            logger.warning("news tz_convert fallback for %r vs %r: %s", a, b, e, exc_info=True)
    return bool(a <= b)


def load_news(
    records: list[dict] | None,
    trade_date: str | pd.Timestamp | None = None,
    snapshot_date: str | pd.Timestamp | None = None,
    available_at: str | pd.Timestamp | None = None,
    snapshot: str | pd.Timestamp | None = None,
    allow_synthetic: bool = False,
    historical_mode: bool = False,
    **kwargs,
) -> list[dict]:
    """按 trade_date 过滤并标注 PIT。

    参数:
        records: 新闻记录列表，每条为 dict，需含 trade_date 与发布时间字段
        trade_date: 目标交易日，过滤 trade_date 相同者；None 时不过滤
        snapshot_date/available_at/snapshot: PIT 快照时间（彼此互为别名，
            优先级 snapshot_date > available_at > snapshot > kwargs 兜底
            snapshot_time/avail_at/pit_snapshot）
        allow_synthetic: 显式允许合成/离线使用缺 available_at 的非 PIT 数据
           （仅作标记确认，不将缺 available_at 记录提升为 live 可用）
        historical_mode: 显式历史离线模式（同 allow_synthetic 的确认语义）
    返回:
        新列表（浅拷贝+新增 pit/pit_status/available_at/provenance/
        usable_for_live），不修改原 records。
        规则：仅当可验证发布时间且 publish_time ≤ 全局快照时 pit=True，否则 False；
        pit_status: verified(可验且通过) / future(发布时间晚于快照或记录级
        available_at 晚于全局快照) / unknown|unavailable(缺失)
        缺全局快照时全为 pit=False + usable_for_live=False（fail-closed 标记，
        不当 live/回测用）；缺记录级 available_at 时 pit 按 publish 照判（兼容
        存量），但 usable_for_live=False（缺 available_at 不被当 live 用）。
        每条返回记录必带 available_at（记录级优先，否则全局快照，否则 None）
        与 provenance.available_at / provenance.snapshot_source。
    异常:
        ValueError: 全局快照或记录级 available_at 非法时间（严格解析，不静默）；
            bias guard — 当缺失 trade_date 的记录超过 50% 时抛出（schema 漂移保护）；
        trade_date 列整体缺失时仅告警并返回空列表。
    """
    # 兼容调用方经 kwargs 传入显式模式标记
    if "allow_synthetic" in kwargs and not allow_synthetic:
        allow_synthetic = bool(kwargs.pop("allow_synthetic"))
    elif "allow_synthetic" in kwargs:
        kwargs.pop("allow_synthetic")
    if "historical_mode" in kwargs and not historical_mode:
        historical_mode = bool(kwargs.pop("historical_mode"))
    elif "historical_mode" in kwargs:
        kwargs.pop("historical_mode")

    # 统一别名优先级：snapshot_date > available_at > snapshot > kwargs 兜底
    eff_snapshot_raw, snapshot_source = _pick_snapshot_raw(
        snapshot_date, available_at, snapshot, kwargs
    )
    # 弹出已消费的 kwargs 兜底别名，避免未知 kwargs 残留
    for alias in ("snapshot_time", "avail_at", "pit_snapshot"):
        kwargs.pop(alias, None)

    if eff_snapshot_raw is None:
        global_snapshot = None
    else:
        # T4-4：快照时间非法直接抛错，不静默标 unknown
        global_snapshot = _parse_time_strict(eff_snapshot_raw, field="available_at")

    # 兼容 trade_date 经 kwargs 传入
    if trade_date is None and "tradeDate" in kwargs:
        trade_date = kwargs.pop("tradeDate")

    target_date_str = _normalize_date_str(trade_date) if trade_date is not None else None

    if not records:
        return []

    # Pre-check schema anomaly: if trade_date filter requested but no record has trade_date at all
    if target_date_str is not None:
        has_any_trade_date = False
        for r in records:
            if isinstance(r, dict) and _extract_trade_date(r) is not None:
                has_any_trade_date = True
                break
        if not has_any_trade_date and len(records) > 0:
            logger.warning("news schema anomaly: no trade_date column for filter %r", target_date_str)
            return []

    out: list[dict] = []
    dropped_missing = 0
    dropped_mismatch = 0
    dropped_non_dict = 0
    total = len(records) if isinstance(records, (list, tuple)) else 0

    for rec in records:
        if not isinstance(rec, dict):
            dropped_non_dict += 1
            logger.warning("news load_news dropped non-dict record: %r", rec)
            continue
        # trade_date 过滤 with accounting
        if target_date_str is not None:
            rec_date = _extract_trade_date(rec)
            if rec_date is None:
                dropped_missing += 1
                continue
            if rec_date != target_date_str:
                dropped_mismatch += 1
                continue

        # 拷贝避免污染（浅拷贝即可）
        new_rec = dict(rec)

        pub_ts = _extract_publish_time(rec)
        snap_ts = _resolve_snapshot_for_record(rec, global_snapshot)

        # 记录级 available_at：存在但非法直接抛错（不静默）；缺失则兼容存量。
        rec_avail_ts, rec_avail_src, rec_avail_malformed = _extract_available_at(rec)
        if rec_avail_malformed:
            raise ValueError(
                f"news record {rec_avail_src} unparseable {rec.get(rec_avail_src)!r} (fail-closed)"
            )

        rec_avail_iso = rec_avail_ts.isoformat() if rec_avail_ts is not None else None
        global_iso = global_snapshot.isoformat() if global_snapshot is not None else None

        # 记录级 snapshot_date/available_at/snapshot 与 publish_time 同为该条的
        # 数据可得时间候选：publish 不得晚于记录级 available_at，否则未来函数嫌疑。
        if pub_ts is not None and rec_avail_ts is not None:
            try:
                if not _tz_compare_le(pub_ts, rec_avail_ts):
                    new_rec["pit"] = False
                    new_rec["pit_status"] = "future"
                    new_rec["usable_for_live"] = False
                    new_rec["available_at"] = rec_avail_iso
                    new_rec["provenance"] = {
                        "available_at": new_rec["available_at"],
                        "snapshot_source": rec_avail_src,
                        "global_snapshot": global_iso,
                        "usable_for_live": False,
                    }
                    out.append(new_rec)
                    continue
            except (TypeError, ValueError):
                new_rec["pit"] = False
                new_rec["pit_status"] = "unavailable"
                new_rec["usable_for_live"] = False
                new_rec["available_at"] = rec_avail_iso
                new_rec["provenance"] = {
                    "available_at": new_rec["available_at"],
                    "snapshot_source": rec_avail_src,
                    "global_snapshot": global_iso,
                    "usable_for_live": False,
                }
                out.append(new_rec)
                continue

        if pub_ts is None or snap_ts is None:
            new_rec["pit"] = False
            # 诚实状态：unknown/unavailable
            if pub_ts is None and snap_ts is None:
                new_rec["pit_status"] = "unknown"
            elif pub_ts is None:
                new_rec["pit_status"] = "unknown"
            else:
                new_rec["pit_status"] = "unavailable"
        else:
            try:
                if _tz_compare_le(pub_ts, snap_ts):
                    new_rec["pit"] = True
                    new_rec["pit_status"] = "verified"
                else:
                    new_rec["pit"] = False
                    new_rec["pit_status"] = "future"
            except (TypeError, ValueError):
                # incomparable timezone state -> honest non-PIT, do not forge
                new_rec["pit"] = False
                new_rec["pit_status"] = "unavailable"

        # T4-4：记录级 available_at 晚于全局快照 -> 未来函数嫌疑，强制 future。
        if (
            new_rec.get("pit") is True
            and rec_avail_ts is not None
            and global_snapshot is not None
        ):
            try:
                if not _tz_compare_le(rec_avail_ts, global_snapshot):
                    new_rec["pit"] = False
                    new_rec["pit_status"] = "future"
            except (TypeError, ValueError):
                new_rec["pit"] = False
                new_rec["pit_status"] = "unavailable"

        # T4-4 收口：available_at 为必填 PIT 字段（标记式 fail-closed，兼容存量）。
        # 缺全局快照 / 缺记录级 available_at 的新闻一律 pit 照判（兼容存量），但
        # usable_for_live=False（缺 available_at 不被当 live/回测用）；离线合成
        # 使用需显式 allow_synthetic=True 或 historical_mode=True 确认（仅作调用
        # 方确认标记，不将 usable_for_live 提升为 True）。
        explicit_offline = bool(allow_synthetic or historical_mode)
        missing_global = global_snapshot is None
        missing_record_avail = rec_avail_ts is None
        if missing_global or missing_record_avail:
            if not explicit_offline:
                logger.warning(
                    "news PIT fail-closed mark: %s (pit=False-equivalent for live; "
                    "explicit allow_synthetic/historical_mode required for offline use)",
                    "missing global snapshot (available_at)"
                    if missing_global
                    else "missing record available_at",
                )
            new_rec["usable_for_live"] = False
        else:
            new_rec["usable_for_live"] = bool(new_rec.get("pit") is True)

        # 返回 provenance/记录必带 available_at（记录级优先，否则全局快照）。
        new_rec["available_at"] = rec_avail_iso if rec_avail_iso is not None else global_iso
        new_rec["provenance"] = {
            "available_at": new_rec["available_at"],
            "snapshot_source": rec_avail_src if rec_avail_src is not None else snapshot_source,
            "global_snapshot": global_iso,
            "usable_for_live": bool(new_rec.get("usable_for_live", False)),
        }

        out.append(new_rec)

    # log dropped counts with reasons at warning; bias guard
    if target_date_str is not None:
        dropped_total = dropped_missing + dropped_mismatch + dropped_non_dict
        if dropped_total > 0:
            logger.warning(
                "news trade_date filtering dropped %d/%d rows for target %s: missing_trade_date=%d mismatch=%d non_dict=%d kept=%d",
                dropped_total,
                total,
                target_date_str,
                dropped_missing,
                dropped_mismatch,
                dropped_non_dict,
                len(out),
            )
        # bias guard: missing trade_date >50% indicates schema/bias issue, raise
        if total > 0 and dropped_missing / total > 0.5:
            raise ValueError(
                f"trade_date filtering dropped >50% due to missing trade_date: {dropped_missing}/{total} "
                f"for target {target_date_str!r} (bias guard)"
            )

    return out


def _disclosure_text(records: list[dict] | None) -> str:
    """内部：根据已标注记录生成披露文本（含 available_at 缺失提示）。"""
    if not records:
        return "non-PIT source/unavailable - no verified news snapshot (PIT unavailable)"

    total = len(records)
    # 窄化：仅对 dict 记录统计，避免字符串 in 误判
    pit_true = sum(1 for r in records if isinstance(r, dict) and r.get("pit") is True)
    pit_false = total - pit_true
    # 若含 unknown/unavailable 统计
    unknown = sum(1 for r in records if isinstance(r, dict) and r.get("pit_status") in ("unknown", "unavailable", "missing"))
    verified = pit_true
    missing_avail = sum(1 for r in records if isinstance(r, dict) and not r.get("available_at"))
    not_live = sum(1 for r in records if isinstance(r, dict) and r.get("usable_for_live") is not True)
    suffix = ""
    if missing_avail or not_live:
        suffix = f" [available_at missing {missing_avail}/{total}; not for live {not_live}/{total}]"

    if pit_false == total:
        # 全为非 PIT
        if unknown:
            return f"non-PIT source/unavailable - {pit_false}/{total} records without verified PIT timestamp (unknown/unavailable){suffix}"
        return f"non-PIT source/unavailable - {pit_false}/{total} records not PIT-verified (publish > snapshot){suffix}"
    if pit_false > 0:
        return f"PIT verified {verified}/{total}; non-PIT source/unavailable {pit_false}/{total} (future/unknown excluded from PIT){suffix}"

    return f"PIT verified {verified}/{total}; non-PIT source/unavailable 0/{total}{suffix}"


def get_disclosure(records: list[dict] | None = None, **kwargs) -> str:
    """对外披露 helper：接受已过滤记录列表，返回含 non-PIT 提示的文本。"""
    # 兼容部分调用者传入 None 或未标注记录：若记录无 pit 字段则视为 non-PIT
    # 统一 news kwargs：支持 news / news_records / filtered 别名
    if records is None:
        for key in ("filtered", "news", "news_records", "newsRecords"):
            if key in kwargs and kwargs[key] is not None:
                records = kwargs[key]
                break
        else:
            records = []
    # 若记录未含 pit 字段，诚实视为 unavailable；窄化 isinstance 避免字符串 in 误判
    if records and not any(isinstance(r, dict) and "pit" in r for r in records):
        return "non-PIT source/unavailable - PIT status not verified"
    return _disclosure_text(records)


def build_disclosure(records: list[dict] | None = None, **kwargs) -> str:
    return get_disclosure(records, **kwargs)


def format_disclosure(records: list[dict] | None = None, **kwargs) -> str:
    return get_disclosure(records, **kwargs)


def get_pit_disclosure(records: list[dict] | None = None, **kwargs) -> str:
    return get_disclosure(records, **kwargs)


def build_news_disclosure(records: list[dict] | None = None, **kwargs) -> str:
    return get_disclosure(records, **kwargs)


def news_disclosure(records: list[dict] | None = None, **kwargs) -> str:
    return get_disclosure(records, **kwargs)
