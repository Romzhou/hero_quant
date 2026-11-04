"""data.quality — 数据质量门（PR-123）：坏数据阻断 + 机器可读 reason。

职责边界
--------
`data.snapshot.normalize_bars()` 已做**逐根字段级**校验（字段齐全/有限/OHLC 一致/
落在 [start, end]/同日重复）。本模块不重复那套判定，只补它覆盖不到的**时序与跨源
层面**：

1. 完整性：应有交易日的缺口、首尾缺失、整段零成交量（疑似停牌/坏源）；
2. 异常值：单日涨跌幅越界、连续多根收盘不变（疑似串行/缓存）、负成交量、
   价格非有限/非正（纵深防御，防绕过 normalize_bars 的直接调用）；
3. 跨源一致性：与 `reference` 同日收盘偏离超 tolerance；
4. 交易日历：显式 `calendar` 之外的日期；无 `calendar` 时只查周末。

设计约束
--------
- **纯函数 + dataclass**：零第三方依赖、确定性（同一输入必得同一 report）；
- **只报不修**：不删行、不补值、不插值。坏数据删还是重取由上游决定；
- **不猜节假日**：仓库无交易日历工具，因此「区间应有交易日」的缺口/首尾检查**必须**
  由调用方显式传 `calendar`。用「工作日」估算会把元旦/春节/国庆整段判成缺口，
  假阳性一多，告警就被无视（门变摆设）。但与日历无关的**横截面覆盖率**始终生效：
  节假日对所有标的同等影响，缺根比例互相一比就能暴露（见 `_check_cross_coverage`）；
- **detail 有界**：每个 (code, symbol) 聚合成一条 Finding，detail 只带计数 + 阈值 +
  最多 `thresholds.max_samples` 条样例，绝不回显整表。

机器码（对外契约，改名等于破坏调用方：reason 直接透到 4xx 响应与日志）
------------------------------------------------------------------------
block：
- `symbol_missing`         请求的标的没有任何 bar
- `bar_malformed`          行不是 Mapping / 缺 symbol / 缺日期 / 缺 close|volume
- `coverage_gap`           区间内应有交易日缺失比例 > max_gap_ratio
- `coverage_gap_vs_peers`  横截面覆盖率 < min_cross_coverage（无需日历）
- `missing_head`           区间首个交易日缺失
- `missing_tail`           区间最后一个交易日缺失
- `price_not_finite`       close 为 NaN/±inf
- `price_not_positive`     close <= 0
- `volume_not_finite`      volume 为 NaN/±inf
- `negative_volume`        volume < 0
- `return_outlier`         单日收盘涨跌幅绝对值 > max_abs_return
- `flat_close_run`         连续 >= max_flat_run 根收盘完全相同（成交量非零时）
- `cross_source_divergence` 同日收盘与 reference 偏离 > cross_source_tolerance
- `calendar_violation`     bar 日期不在显式 calendar 内
- `weekend_bar`            无 calendar 时，bar 落在周六/周日
warn：
- `coverage_partial_vs_peers` 横截面覆盖率落在 [min_cross_coverage, partial_coverage)
- `volume_all_zero`        整个标的区间成交量恒为 0（停牌/坏源，只报不删）
- `flat_close_run`         同上但区间成交量全为 0（更像停牌而非缓存）
- `cross_source_no_overlap` 与 reference 无任何共同交易日（无法比对）
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from itertools import islice, pairwise
from typing import Any

SEVERITY_BLOCK = "block"
SEVERITY_WARN = "warn"

_SEVERITY_RANK = {SEVERITY_WARN: 0, SEVERITY_BLOCK: 1}
_WEEKEND_START = 5  # date.weekday(): 0=周一 … 5=周六, 6=周日


@dataclass(frozen=True, slots=True)
class QualityThresholds:
    """质量门口径（全部可参数化，不读 env：新增 env 需三处联动，本 PR 不批）。

    默认值理由见 `check_bars` 各检查段落注释。
    """

    max_gap_ratio: float = 0.05
    max_abs_return: float = 0.5
    max_flat_run: int = 5
    cross_source_tolerance: float = 0.005
    min_cross_coverage: float = 0.5
    partial_coverage: float = 0.9
    min_union_days: int = 5
    max_samples: int = 5

    def __post_init__(self) -> None:
        if not 0.0 <= self.max_gap_ratio <= 1.0:
            raise ValueError("max_gap_ratio_out_of_range")
        if not self.max_abs_return > 0:
            raise ValueError("max_abs_return_must_be_positive")
        if self.max_flat_run < 2:
            raise ValueError("max_flat_run_must_be_at_least_2")
        if self.cross_source_tolerance < 0:
            raise ValueError("cross_source_tolerance_must_be_non_negative")
        if not 0.0 <= self.min_cross_coverage <= 1.0:
            raise ValueError("min_cross_coverage_out_of_range")
        if not 0.0 <= self.partial_coverage <= 1.0:
            raise ValueError("partial_coverage_out_of_range")
        if self.partial_coverage < self.min_cross_coverage:
            raise ValueError("partial_coverage_below_min_cross_coverage")
        if self.min_union_days < 1:
            raise ValueError("min_union_days_must_be_at_least_1")
        if self.max_samples < 1:
            raise ValueError("max_samples_must_be_at_least_1")


@dataclass(frozen=True, slots=True)
class Finding:
    """单条质量问题；同一 (code, symbol) 聚合，Findings 数量与表长无关。

    code       稳定机器码（见模块 docstring 的码表）
    severity   "block"（阻断）/ "warn"（只报）
    symbol     标的；表级问题（行无法归属到标的）为 None
    trade_date 该聚合内最早命中的交易日；无日期语义时为 None
    detail     计数 + 阈值 + 至多样例（禁止整表回显）
    count      受影响 bar 数（聚合内的命中次数）
    """

    code: str
    severity: str
    symbol: str | None = None
    trade_date: date | None = None
    detail: str = ""
    count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "symbol": self.symbol,
            "trade_date": self.trade_date.isoformat() if self.trade_date else None,
            "detail": self.detail,
            "count": self.count,
        }


def _finding_key(finding: Finding) -> tuple[str, date, str]:
    """确定性排序：symbol, date, code（reason 取排序后的首个 block）。"""
    return (finding.symbol or "", finding.trade_date or date.min, finding.code)


@dataclass(frozen=True, slots=True)
class QualityReport:
    """一次质量门的完整结果；`assert_quality` 只在存在 block 时抛错。"""

    findings: tuple[Finding, ...] = ()
    checked_rows: int = 0
    checked_symbols: tuple[str, ...] = ()
    thresholds: QualityThresholds = field(default_factory=QualityThresholds)

    @property
    def blocks(self) -> tuple[Finding, ...]:
        return tuple(sorted((f for f in self.findings if f.severity == SEVERITY_BLOCK), key=_finding_key))

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(sorted((f for f in self.findings if f.severity == SEVERITY_WARN), key=_finding_key))

    @property
    def ok(self) -> bool:
        return not self.blocks

    @property
    def reason(self) -> str | None:
        """首个 block 的稳定 code（排序确定性：symbol, date, code）。"""
        blocks = self.blocks
        return blocks[0].code if blocks else None

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(sorted({f.code for f in self.blocks}))

    def detail(self, max_samples: int | None = None) -> str:
        """计数 + 至多 N 条 block 样例；绝不回显整表。"""
        limit = self.thresholds.max_samples if max_samples is None else max(1, int(max_samples))
        blocks = self.blocks
        codes = ",".join(sorted({finding.code for finding in blocks})) or "none"
        head = f"rows={self.checked_rows} block={len(blocks)} warn={len(self.warnings)} codes={codes}"
        samples = [
            f"{finding.code}:{finding.symbol or '*'}:{finding.trade_date.isoformat() if finding.trade_date else '*'}"
            for finding in blocks[:limit]
        ]
        return f"{head}; first={','.join(samples)}" if samples else head

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "block_codes": list(self.codes),
            "checked_rows": self.checked_rows,
            "checked_symbols": list(self.checked_symbols),
            "thresholds": asdict(self.thresholds),
            "findings": [finding.to_dict() for finding in self.findings],
        }


class DataQualityError(RuntimeError):
    """质量门阻断；`reason` 是稳定机器码，`detail` 有界（计数 + 样例）。"""

    error = "data_quality_error"
    status_code = 400

    def __init__(self, reason: str, detail: str = "", findings: Sequence[Finding] = ()) -> None:
        self.reason = reason
        self.detail = detail
        self.findings = tuple(findings)
        self.codes = tuple(sorted({finding.code for finding in self.findings}))
        message = f"{self.error}: {reason}"
        if detail:
            message += f" ({detail})"
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.error,
            "reason": self.reason,
            "codes": list(self.codes),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class _Point:
    """一根 bar 里质量门关心的部分（OHLC 一致性由 normalize_bars 负责）。"""

    trade_date: date
    close: float | None
    volume: float | None


@dataclass
class _Acc:
    symbol: str | None
    code: str
    severity: str
    first_date: date | None = None
    note: str = ""
    samples: list[str] = field(default_factory=list)
    count: int = 0


class _FindingSink:
    """按 (code, symbol) 聚合，保证 Finding 条数与表长无关（禁止整表回显）。"""

    def __init__(self, max_samples: int) -> None:
        self._max_samples = max(1, int(max_samples))
        self._acc: dict[tuple[str, str], _Acc] = {}

    def add(
        self,
        code: str,
        severity: str,
        symbol: str | None,
        trade_date: date | None,
        *,
        note: str = "",
        samples: Sequence[str] = (),
        count: int = 1,
    ) -> None:
        if severity not in _SEVERITY_RANK:
            raise ValueError(f"unknown_severity:{severity}")
        key = (symbol or "", code)
        acc = self._acc.get(key)
        if acc is None:
            acc = _Acc(symbol=symbol, code=code, severity=severity)
            self._acc[key] = acc
        elif _SEVERITY_RANK[severity] > _SEVERITY_RANK[acc.severity]:
            # 同一 (code, symbol) 只保留最严的等级，避免聚合后告警被洗白。
            acc.severity = severity
        acc.count += max(1, int(count))
        if trade_date is not None and (acc.first_date is None or trade_date < acc.first_date):
            acc.first_date = trade_date
        if note and not acc.note:
            acc.note = note
        for sample in samples:
            if len(acc.samples) >= self._max_samples:
                break
            acc.samples.append(sample)

    def build(self) -> tuple[Finding, ...]:
        findings = [self._to_finding(acc) for acc in self._acc.values()]
        findings.sort(key=_finding_key)
        return tuple(findings)

    @staticmethod
    def _to_finding(acc: _Acc) -> Finding:
        parts = [f"count={acc.count}"]
        if acc.note:
            parts.append(acc.note)
        if acc.samples:
            parts.append("samples=" + ", ".join(acc.samples))
        return Finding(
            code=acc.code,
            severity=acc.severity,
            symbol=acc.symbol,
            trade_date=acc.first_date,
            detail="; ".join(parts),
            count=acc.count,
        )


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _symbol_list(symbols: Sequence[str] | None) -> tuple[str, ...]:
    if symbols is None:
        return ()
    if isinstance(symbols, (str, bytes)):
        raise ValueError("symbols_must_be_sequence_of_str")
    out: list[str] = []
    for item in symbols:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("symbol_must_be_non_empty_str")
        out.append(item.strip())
    return tuple(out)


def _calendar_days(calendar: Iterable[date | datetime | str]) -> frozenset[date]:
    if isinstance(calendar, (str, bytes)):
        raise ValueError("calendar_must_be_sequence_of_dates")
    days: set[date] = set()
    for item in calendar:
        day = _coerce_date(item)
        if day is None:
            raise ValueError("calendar_invalid_date")
        days.add(day)
    if not days:
        raise ValueError("calendar_empty")
    return frozenset(days)


def _reference_closes(reference: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, dict[date, float]]:
    """参考源 (symbol -> rows) 压成 symbol -> {trade_date: close}。

    reference 内解析不出日期/收盘的行不参与比对（被测侧才是要拦的数据）。
    """
    if not isinstance(reference, Mapping):
        raise ValueError("reference_must_be_mapping")
    out: dict[str, dict[date, float]] = {}
    for raw_symbol, ref_rows in reference.items():
        if not isinstance(raw_symbol, str) or not raw_symbol.strip():
            raise ValueError("reference_symbol_must_be_str")
        closes: dict[date, float] = {}
        for row in ref_rows or ():
            if not isinstance(row, Mapping):
                continue
            trade_date = _coerce_date(row.get("trade_date", row.get("date")))
            close = _as_float(row.get("close"))
            if trade_date is None or close is None or not math.isfinite(close) or close <= 0:
                continue
            closes.setdefault(trade_date, close)
        out[raw_symbol.strip()] = closes
    return out


def check_bars(
    rows: Sequence[Mapping[str, Any]],
    *,
    symbols: Sequence[str] | None = None,
    start: date | datetime | str | None = None,
    end: date | datetime | str | None = None,
    calendar: Iterable[date | datetime | str] | None = None,
    reference: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    thresholds: QualityThresholds | None = None,
    require_volume: bool = True,
) -> QualityReport:
    """对抬平后的 bars 跑完整性/异常值/跨源/日历四类检查，返回 QualityReport。

    `rows` 元素形如 normalize_bars 的产物（symbol/trade_date/close/volume…），也接受
    `date` 键别名。基线样本 2024-01-01(周一)/01-02(周二) 在默认阈值下不触发任何
    block：「区间应有交易日」缺口/首尾需显式 `calendar`（不猜节假日）；横截面覆盖率
    在 union < min_union_days(5) 时跳过，故「短窗口 + 标的根数不齐」不会误伤。
    `start`/`end` 给定时窗口以它为准；未给则窗口取全体 bar 的首尾日期，此时
    `missing_head`/`missing_tail` 不会命中（少了边界就无从判断「首尾」）。

    `require_volume=False` 只用于「数据源本身不提供 volume 维度」的调用方（如
    registry 出口闸遇到只有 close/date 的源）：此时 volume 缺失不再算 `bar_malformed`，
    价格/时序规则照旧生效。默认 True 保持既有调用方（snapshot 入库路径）语义不变——
    逐根缺 volume 在那里仍是坏数据。调用方必须把「volume 维度缺席」显式记账，
    不得用它做静默旁路。
    """
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise ValueError("rows_must_be_sequence")
    thresholds = thresholds or QualityThresholds()
    sink = _FindingSink(thresholds.max_samples)

    window_start = _coerce_date(start) if start is not None else None
    window_end = _coerce_date(end) if end is not None else None
    if start is not None and window_start is None:
        raise ValueError("start_invalid_date")
    if end is not None and window_end is None:
        raise ValueError("end_invalid_date")
    if window_start is not None and window_end is not None and window_start > window_end:
        raise ValueError("start_after_end")

    points: dict[str, list[_Point]] = {}
    data_min: date | None = None
    data_max: date | None = None
    for row in rows:
        if not isinstance(row, Mapping):
            sink.add("bar_malformed", SEVERITY_BLOCK, None, None, note=f"row_not_mapping:{type(row).__name__}")
            continue
        raw_symbol = row.get("symbol")
        if not isinstance(raw_symbol, str) or not raw_symbol.strip():
            sink.add("bar_malformed", SEVERITY_BLOCK, None, None, note="row_without_symbol")
            continue
        symbol = raw_symbol.strip()
        raw_date = row.get("trade_date", row.get("date"))
        trade_date = _coerce_date(raw_date)
        if trade_date is None:
            sink.add("bar_malformed", SEVERITY_BLOCK, symbol, None, note=f"bar_without_trade_date:{raw_date!r}"[:80])
            continue
        close = _as_float(row.get("close"))
        volume = _as_float(row.get("volume"))
        if close is None:
            sink.add("bar_malformed", SEVERITY_BLOCK, symbol, trade_date, note="close_missing",
                     samples=[trade_date.isoformat()])
        elif not math.isfinite(close):
            sink.add("price_not_finite", SEVERITY_BLOCK, symbol, trade_date, note=f"close={close!r}",
                     samples=[trade_date.isoformat()])
        elif close <= 0:
            sink.add("price_not_positive", SEVERITY_BLOCK, symbol, trade_date, note=f"close={close!r}",
                     samples=[trade_date.isoformat()])
        if volume is None:
            if require_volume:
                sink.add("bar_malformed", SEVERITY_BLOCK, symbol, trade_date, note="volume_missing",
                         samples=[trade_date.isoformat()])
        elif not math.isfinite(volume):
            sink.add("volume_not_finite", SEVERITY_BLOCK, symbol, trade_date, note=f"volume={volume!r}",
                     samples=[trade_date.isoformat()])
        elif volume < 0:
            sink.add("negative_volume", SEVERITY_BLOCK, symbol, trade_date, note=f"volume={volume!r}",
                     samples=[trade_date.isoformat()])
        points.setdefault(symbol, []).append(_Point(trade_date, close, volume))
        if data_min is None or trade_date < data_min:
            data_min = trade_date
        if data_max is None or trade_date > data_max:
            data_max = trade_date

    # 缺口窗口：调用方给的 start/end 优先；未给则用全体 bar 的首尾日期。
    gap_start = window_start or data_min
    gap_end = window_end or data_max
    calendar_days = _calendar_days(calendar) if calendar is not None else None
    reference_map = _reference_closes(reference) if reference is not None else None

    requested = _symbol_list(symbols)
    universe = sorted(set(points) | set(requested))
    for symbol in universe:
        series = sorted(points.get(symbol, ()), key=lambda item: item.trade_date)
        if not series:
            sink.add("symbol_missing", SEVERITY_BLOCK, symbol, None, note="no bars for requested symbol")
            continue
        if calendar_days is not None:
            _check_coverage(sink, thresholds, symbol, series, calendar_days, gap_start, gap_end)
            _check_calendar_membership(sink, symbol, series, calendar_days)
        else:
            _check_weekend(sink, symbol, series)
        _check_volume_shape(sink, symbol, series)
        _check_series(sink, thresholds, symbol, series)
        if reference_map is not None and symbol in reference_map:
            _check_reference(sink, thresholds, symbol, series, reference_map[symbol])

    # 横截面覆盖率：不依赖交易日历，是 record_snapshot 路径上完整性的唯一防线。
    _check_cross_coverage(sink, thresholds, points, universe, window_start, window_end)

    return QualityReport(
        findings=sink.build(),
        checked_rows=len(rows),
        checked_symbols=tuple(universe),
        thresholds=thresholds,
    )


def _check_coverage(
    sink: _FindingSink,
    thresholds: QualityThresholds,
    symbol: str,
    series: Sequence[_Point],
    calendar_days: frozenset[date],
    gap_start: date | None,
    gap_end: date | None,
) -> None:
    """缺口/首尾缺失：只在显式 calendar 下判定（无日历不猜节假日）。"""
    if gap_start is None or gap_end is None:
        return
    expected = sorted(day for day in calendar_days if gap_start <= day <= gap_end)
    if not expected:
        return
    actual = {point.trade_date for point in series}
    missing = [day for day in expected if day not in actual]
    if not missing:
        return
    ratio = len(missing) / len(expected)
    if ratio > thresholds.max_gap_ratio:
        sink.add(
            "coverage_gap",
            SEVERITY_BLOCK,
            symbol,
            missing[0],
            note=(
                f"{len(missing)}/{len(expected)} expected trading days missing "
                f"(ratio {ratio:.0%} > {thresholds.max_gap_ratio:.0%})"
            ),
            samples=[day.isoformat() for day in missing[: thresholds.max_samples]],
            count=len(missing),
        )
    if expected[0] not in actual:
        sink.add(
            "missing_head",
            SEVERITY_BLOCK,
            symbol,
            expected[0],
            note=f"first expected trading day {expected[0].isoformat()} absent",
            samples=[expected[0].isoformat()],
        )
    if expected[-1] not in actual:
        sink.add(
            "missing_tail",
            SEVERITY_BLOCK,
            symbol,
            expected[-1],
            note=f"last expected trading day {expected[-1].isoformat()} absent",
            samples=[expected[-1].isoformat()],
        )


def _check_cross_coverage(
    sink: _FindingSink,
    thresholds: QualityThresholds,
    points: Mapping[str, Sequence[_Point]],
    universe: Sequence[str],
    window_start: date | None,
    window_end: date | None,
) -> None:
    """横截面覆盖率：同一窗口内，标的实际交易日 / 全体标的交易日并集。

    为什么不需要交易日历：节假日对所有标的是**同等**影响，先取并集就等于把休市日
    减掉了；某个标的单独缺根才会让它的覆盖率掉下来。所以这条检查既能覆盖
    `record_snapshot`（无日历可用）的完整性，又不会像「工作日估算法」那样把长假
    整段判成缺口。

    门槛：`len(universe) >= 2`（单标的无从比较）且 `|union| >= min_union_days`
    （短窗口无统计意义，也保住既有 2 根/1 根的 fixture）。覆盖率严格小于阈值才升级：
    `coverage < min_cross_coverage` → block；`[min_cross_coverage, partial_coverage)`
    → warn；`>= partial_coverage` 不报。
    """
    if len(universe) < 2:
        return
    union: set[date] = set()
    for symbol in universe:
        for point in points.get(symbol, ()):
            if window_start is not None and point.trade_date < window_start:
                continue
            if window_end is not None and point.trade_date > window_end:
                continue
            union.add(point.trade_date)
    total = len(union)
    if total < thresholds.min_union_days:
        return
    for symbol in universe:
        own = {point.trade_date for point in points.get(symbol, ())} & union
        coverage = len(own) / total
        if coverage >= thresholds.partial_coverage:
            continue
        missing = sorted(union - own)
        # detail 只带 symbol/覆盖率/并集规模（+ 被突破的阈值名），不回显缺根清单。
        note = f"{symbol}: coverage {len(own)}/{total} = {coverage:.0%} of peers union ({total} days), below "
        if coverage < thresholds.min_cross_coverage:
            sink.add(
                "coverage_gap_vs_peers",
                SEVERITY_BLOCK,
                symbol,
                missing[0],
                note=f"{note}min_cross_coverage",
                count=len(missing),
            )
        else:
            sink.add(
                "coverage_partial_vs_peers",
                SEVERITY_WARN,
                symbol,
                missing[0],
                note=f"{note}partial_coverage",
                count=len(missing),
            )


def _check_calendar_membership(
    sink: _FindingSink, symbol: str, series: Sequence[_Point], calendar_days: frozenset[date]
) -> None:
    for point in series:
        if point.trade_date not in calendar_days:
            sink.add(
                "calendar_violation",
                SEVERITY_BLOCK,
                symbol,
                point.trade_date,
                note="trade_date not in provided trading calendar",
                samples=[point.trade_date.isoformat()],
            )


def _check_weekend(sink: _FindingSink, symbol: str, series: Sequence[_Point]) -> None:
    for point in series:
        if point.trade_date.weekday() >= _WEEKEND_START:
            sink.add(
                "weekend_bar",
                SEVERITY_BLOCK,
                symbol,
                point.trade_date,
                note="weekend bar (A 股周末不交易)",
                samples=[point.trade_date.isoformat()],
            )


def _check_volume_shape(sink: _FindingSink, symbol: str, series: Sequence[_Point]) -> None:
    volumes = [point.volume for point in series if point.volume is not None]
    if volumes and all(volume == 0 for volume in volumes):
        sink.add(
            "volume_all_zero",
            SEVERITY_WARN,
            symbol,
            series[0].trade_date,
            note=f"all {len(volumes)} bars volume=0 (suspended or broken source?)",
            samples=[series[0].trade_date.isoformat()],
            count=len(volumes),
        )


def _check_series(sink: _FindingSink, thresholds: QualityThresholds, symbol: str, series: Sequence[_Point]) -> None:
    """单日涨跌幅 + 连续收盘不变。只对 close 有效且 > 0 的根计算。"""
    valid = [point for point in series if point.close is not None and point.close > 0]
    if not valid:
        return
    for previous, current in pairwise(valid):
        change = (current.close - previous.close) / previous.close  # type: ignore[operator]
        if abs(change) > thresholds.max_abs_return:
            sink.add(
                "return_outlier",
                SEVERITY_BLOCK,
                symbol,
                current.trade_date,
                note=(
                    f"close {previous.close:g}->{current.close:g} {change:+.1%} "
                    f"beyond +-{thresholds.max_abs_return:.0%}"
                ),
                samples=[f"{current.trade_date.isoformat()} {change:+.1%}"],
            )
    run: list[_Point] = [valid[0]]
    for point in islice(valid, 1, None):
        if point.close == run[-1].close:
            run.append(point)
            continue
        _flush_flat_run(sink, thresholds, symbol, run)
        run = [point]
    _flush_flat_run(sink, thresholds, symbol, run)


def _flush_flat_run(
    sink: _FindingSink, thresholds: QualityThresholds, symbol: str, run: Sequence[_Point]
) -> None:
    if len(run) < thresholds.max_flat_run:
        return
    volumes = [point.volume for point in run if point.volume is not None]
    suspended = bool(volumes) and all(volume == 0 for volume in volumes)
    # 停牌/占位行（成交量全 0）降级为 warn；成交量非零还不动 = 串行/缓存，直接 block。
    severity = SEVERITY_WARN if suspended else SEVERITY_BLOCK
    suffix = " (volume=0, likely suspended)" if suspended else ""
    sink.add(
        "flat_close_run",
        severity,
        symbol,
        run[0].trade_date,
        note=f"{len(run)} consecutive closes == {run[0].close:g}{suffix}",
        samples=[f"{run[0].trade_date.isoformat()}..{run[-1].trade_date.isoformat()}"],
        count=len(run),
    )


def _check_reference(
    sink: _FindingSink,
    thresholds: QualityThresholds,
    symbol: str,
    series: Sequence[_Point],
    ref_closes: dict[date, float],
) -> None:
    compared = 0
    for point in series:
        if point.close is None:
            continue
        ref_close = ref_closes.get(point.trade_date)
        if ref_close is None:
            continue
        compared += 1
        deviation = abs(point.close - ref_close) / ref_close
        if deviation > thresholds.cross_source_tolerance:
            sink.add(
                "cross_source_divergence",
                SEVERITY_BLOCK,
                symbol,
                point.trade_date,
                note=(
                    f"close {point.close:g} vs reference {ref_close:g} "
                    f"{deviation:.2%} > {thresholds.cross_source_tolerance:.2%}"
                ),
                samples=[f"{point.trade_date.isoformat()} {deviation:.2%}"],
            )
    if compared == 0:
        sink.add(
            "cross_source_no_overlap",
            SEVERITY_WARN,
            symbol,
            None,
            note="no common trade_date with reference",
        )


def assert_quality(report: QualityReport) -> QualityReport:
    """存在 block 级 finding 就抛 DataQualityError（reason=排序后首个 code），否则原样返回。"""
    if not isinstance(report, QualityReport):
        raise TypeError("report_must_be_quality_report")
    blocks = report.blocks
    if not blocks:
        return report
    raise DataQualityError(report.reason or "data_quality_blocked", report.detail(), findings=blocks)


__all__ = [
    "SEVERITY_BLOCK",
    "SEVERITY_WARN",
    "DataQualityError",
    "Finding",
    "QualityReport",
    "QualityThresholds",
    "assert_quality",
    "check_bars",
]
