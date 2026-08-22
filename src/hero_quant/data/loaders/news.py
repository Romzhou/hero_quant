"""PIT 新闻快照加载器：按 trade_date 过滤 + PIT 诚实标注。

职责：离线/合成新闻记录的 PIT 过滤，不联网，不伪造 PIT。
核心规则：只有存在可验证发布时间且发布时间 ≤ 快照时间才 pit=True，否则 pit=False；
缺失时间戳时 pit_status 为 unknown/unavailable，绝不伪装为 PIT。
"""

from __future__ import annotations

import copy

import pandas as pd

__all__ = [
    "load_news",
    "get_disclosure",
    "build_disclosure",
    "format_disclosure",
    "get_pit_disclosure",
    "build_news_disclosure",
    "news_disclosure",
]

# 候选发布时间字段（按优先级）
_PUBLISH_KEYS = (
    "publish_time",
    "published_at",
    "published_time",
    "publish_date",
    "published",
    "timestamp",
    "datetime",
    "time",
    "date",
)

# 快照字段候选（记录级）
_SNAPSHOT_KEYS = (
    "snapshot_date",
    "snapshot_time",
    "available_at",
    "avail_at",
    "snapshot",
)


def _parse_time(value) -> pd.Timestamp | None:
    """宽松解析时间为 Timestamp，失败返回 None。"""
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
    except Exception:
        return None


def _extract_publish_time(record: dict) -> pd.Timestamp | None:
    for k in _PUBLISH_KEYS:
        if k in record and record[k] is not None and str(record[k]).strip() != "":
            ts = _parse_time(record[k])
            if ts is not None:
                return ts
    return None


def _normalize_date_str(value) -> str | None:
    """归一化为 YYYY-MM-DD 字符串用于 trade_date 过滤。"""
    if value is None:
        return None
    ts = _parse_time(value)
    if ts is None:
        # 回落为字符串去空格比较
        s = str(value).strip()
        return s[:10] if s else None
    try:
        return ts.strftime("%Y-%m-%d")
    except Exception:
        return str(value).strip()[:10]


def _extract_trade_date(record: dict) -> str | None:
    for k in ("trade_date", "trading_date", "tradeDate", "date"):
        if k in record and record[k] is not None:
            v = _normalize_date_str(record[k])
            if v:
                return v
    return None


def _resolve_snapshot_for_record(
    record: dict,
    global_snapshot: pd.Timestamp | None,
) -> pd.Timestamp | None:
    """仅使用全局快照；记录级 snapshot 忽略以避免伪造 PIT。"""
    return global_snapshot


def load_news(
    records: list[dict] | None,
    trade_date: str | pd.Timestamp | None = None,
    snapshot_date: str | pd.Timestamp | None = None,
    available_at: str | pd.Timestamp | None = None,
    snapshot: str | pd.Timestamp | None = None,
    **kwargs,
) -> list[dict]:
    """按 trade_date 过滤并标注 PIT。

    参数:
        records: 新闻记录列表，每条为 dict，需含 trade_date 与发布时间字段
        trade_date: 目标交易日，过滤 trade_date 相同者；None 时不过滤
        snapshot_date/available_at/snapshot: PIT 快照时间（彼此互为别名）
    返回:
        新列表（浅拷贝+新增 pit/pit_status），不修改原 records。
        规则：仅当可验证发布时间且 publish_time ≤ snapshot 时 pit=True，否则 False；
        pit_status: verified(可验且通过) / future(发布时间晚于快照) / unknown|unavailable(缺失)
    """
    # 兼容别名：snapshot_date 优先，其次 available_at、snapshot、kwargs
    eff_snapshot_raw = snapshot_date
    if eff_snapshot_raw is None:
        eff_snapshot_raw = available_at
    if eff_snapshot_raw is None:
        eff_snapshot_raw = snapshot
    # kwargs 兜底
    for alias in ("snapshot_time", "avail_at", "pit_snapshot"):
        if eff_snapshot_raw is None and alias in kwargs:
            eff_snapshot_raw = kwargs.pop(alias)

    global_snapshot = _parse_time(eff_snapshot_raw) if eff_snapshot_raw is not None else None

    # 兼容 trade_date 经 kwargs 传入
    if trade_date is None and "tradeDate" in kwargs:
        trade_date = kwargs.pop("tradeDate")

    target_date_str = _normalize_date_str(trade_date) if trade_date is not None else None

    if not records:
        return []

    out: list[dict] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        # trade_date 过滤
        if target_date_str is not None:
            rec_date = _extract_trade_date(rec)
            if rec_date is None:
                # 若记录无 trade_date 则尝试用 publish_time 的日期推断？保守：直接跳过
                continue
            if rec_date != target_date_str:
                continue

        # 拷贝避免污染
        new_rec = copy.copy(rec)
        # 也可深拷贝浅层值无需 deepcopy
        # 但确保 pit 字段写入新对象
        new_rec = dict(rec)

        pub_ts = _extract_publish_time(rec)
        snap_ts = _resolve_snapshot_for_record(rec, global_snapshot)

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
                # timezone handling: mixed naive/aware is incomparable -> honest unavailable
                def _is_aware(ts: pd.Timestamp) -> bool:
                    try:
                        tz = getattr(ts, "tz", None)
                        if tz is not None:
                            return True
                    except Exception:
                        pass
                    return getattr(ts, "tzinfo", None) is not None

                pub_aware = _is_aware(pub_ts)
                snap_aware = _is_aware(snap_ts)
                if pub_aware != snap_aware:
                    raise TypeError("mixed tz-naive/aware")

                if pub_aware and snap_aware:
                    # normalize both to UTC for correct offset comparison
                    try:
                        pub_cmp = pub_ts.tz_convert("UTC")
                        snap_cmp = snap_ts.tz_convert("UTC")
                    except Exception:
                        # fallback: pandas can compare aware with different tz natively
                        pub_cmp = pub_ts
                        snap_cmp = snap_ts
                else:
                    pub_cmp = pub_ts
                    snap_cmp = snap_ts

                if pub_cmp <= snap_cmp:
                    new_rec["pit"] = True
                    new_rec["pit_status"] = "verified"
                else:
                    new_rec["pit"] = False
                    new_rec["pit_status"] = "future"
            except (TypeError, ValueError):
                # incomparable timezone state -> honest non-PIT, do not forge
                new_rec["pit"] = False
                new_rec["pit_status"] = "unavailable"

        out.append(new_rec)

    return out


def _disclosure_text(records: list[dict] | None) -> str:
    """内部：根据已标注记录生成披露文本。"""
    if not records:
        return "non-PIT source/unavailable - no verified news snapshot (PIT unavailable)"

    total = len(records)
    pit_true = sum(1 for r in records if r.get("pit") is True)
    pit_false = total - pit_true
    # 若含 unknown/unavailable 统计
    unknown = sum(1 for r in records if r.get("pit_status") in ("unknown", "unavailable", "missing"))
    verified = pit_true

    if pit_false == total:
        # 全为非 PIT
        if unknown:
            return f"non-PIT source/unavailable - {pit_false}/{total} records without verified PIT timestamp (unknown/unavailable)"
        return f"non-PIT source/unavailable - {pit_false}/{total} records not PIT-verified (publish > snapshot)"
    if pit_false > 0:
        return f"PIT verified {verified}/{total}; non-PIT source/unavailable {pit_false}/{total} (future/unknown excluded from PIT)"

    return f"PIT verified {verified}/{total}; non-PIT source/unavailable 0/{total}"


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
    # 若记录未含 pit 字段，诚实视为 unavailable
    if records and not any("pit" in r for r in records):
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
