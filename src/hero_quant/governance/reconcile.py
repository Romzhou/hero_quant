"""reconcile — 影子账本与券商持仓日终对账。

职责：以影子流水为基准，比对券商 positions.csv，输出 0 差额校验与差异明细。
架构位置：治理层离线对账，复用 Ledger 与 ShadowJournal 聚合持仓。
关键设计：按 symbol 聚合净持仓（含买卖方向符号）、CSV 表头兼容多别名、重复 symbol 累加；以 tolerance 判定零差额，total_diff 为绝对差之和；文件入口同时校验 ledger 完整性并受 wall-time budget 约束。
"""
from __future__ import annotations
import logging
import math

import csv
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List
logger = logging.getLogger("hero_quant.governance.reconcile")


@dataclass
class ReconcileResult:
    """对账结果：影子/券商持仓快照、逐 symbol 差异、是否零差额及总绝对差。"""

    shadow: Dict[str, float]
    positions: Dict[str, float]
    diffs: List[Dict[str, Any]]
    zero_diff: bool
    total_diff: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_BUY_SIDES = frozenset({"buy", "long", "bid", "cover"})
_SELL_SIDES = frozenset({"sell", "short", "ask"})

# 可扩展卖出别名（仍显式白名单，未命中一律 fail-closed，避免默认买入反转符号）
_SELL_ALIASES = frozenset({"sold", "s", "to_close", "close", "sell_to_close", "sell-to-close"})


def _normalize_qty(value: Any) -> float:
    """数量归一化：空值/非数值抛 ValueError 并 warning，避免脏数据静默。"""
    if value is None:
        logger.warning("invalid qty: empty None")
        raise ValueError("qty is empty")
    if isinstance(value, str) and not value.strip():
        logger.warning("invalid qty: empty string %r", value)
        raise ValueError("qty is empty")
    try:
        fv = float(value)
    except (ValueError, TypeError) as exc:
        logger.warning("invalid qty value %r: %s", value, exc)
        raise ValueError(f"invalid qty: {value!r}") from exc
    if not math.isfinite(fv):
        logger.warning("invalid qty non-finite %r", value)
        raise ValueError(f"invalid qty (non-finite): {value!r}")
    return fv


def load_positions_csv(path: str | Path) -> Dict[str, float]:
    """解析券商 positions.csv，兼容多表头别名并对重复 symbol 累加。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"positions.csv not found: {p}")
    out: Dict[str, float] = {}
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError("positions.csv missing header")
        # normalize header lower
        lower_map = {k.lower().strip(): k for k in reader.fieldnames}
        # detect symbol key
        sym_key = None
        for cand in ["symbol", "instrument", "code", "ticker", "asset"]:
            if cand in lower_map:
                sym_key = lower_map[cand]
                break
        if sym_key is None:
            # 中文：未知表头必须 fail-closed（symbol/quantity 双列），不可位置兜底误映射
            raise ValueError(f"positions.csv missing symbol header, got {reader.fieldnames!r}")
        qty_key = None
        for cand in ["qty", "quantity", "position", "amount", "shares", "holding", "vol"]:
            if cand in lower_map:
                qty_key = lower_map[cand]
                break
        if qty_key is None:
            # 中文：未知表头必须 fail-closed（symbol/quantity 双列），不可位置兜底误映射
            raise ValueError(f"positions.csv missing quantity header, got {reader.fieldnames!r}")

        for row in reader:
            sym = str(row.get(sym_key, "")).strip()
            if not sym:
                logger.warning("load_positions_csv skip blank-symbol row %r", row)
                continue
            qty_raw = row.get(qty_key, 0)
            qty = _normalize_qty(qty_raw)
            # sum duplicate symbols
            out[sym] = out.get(sym, 0) + qty
    return out


def _shadow_qty_from_trade(trade: Dict[str, Any]) -> tuple[str, float]:
    """从单笔影子交易提取 (symbol, signed_qty)，卖出记为负以保留净持仓语义。"""
    sym = str(trade.get("symbol", trade.get("instrument", trade.get("code", "")))).strip()
    if not sym:
        logger.warning("shadow trade missing symbol, skipped: %r", trade)
        return "", 0.0
    qty = trade.get("qty", trade.get("quantity", trade.get("amount", 0)))
    q = _normalize_qty(qty)
    # 中文：显式空 side（None/空串/空白）不可默认买入，fail-closed；
    # 键缺失沿用历史默认 buy（存量影子/ledger 记录多无 side 键，改默认值会断现有流水）。
    _side_raw = trade.get("side", None) if "side" in trade else "buy"
    if _side_raw is None or (isinstance(_side_raw, str) and not _side_raw.strip()):
        logger.warning("shadow trade missing side for symbol %r", sym)
        raise ValueError(f"missing trade side for symbol {sym!r}")
    side = str(_side_raw).strip().lower()
    if side in _SELL_SIDES or side in _SELL_ALIASES:
        # 卖出以负数计入净持仓，便于与券商净持仓直接比对
        q = -abs(q)
    elif side not in _BUY_SIDES:
        # 中文：未知 side 必须 fail-closed，不可默认买入（符号反转）
        logger.warning("unknown trade side %r for symbol %r", trade.get("side"), sym)
        raise ValueError(f"unknown trade side: {trade.get('side')!r}")
    return sym, q


def aggregate_shadow(
    journal: Any | None = None,
    ledger_path: str | Path | None = None,
    ledger: Any | None = None,
    *,
    verify: bool = True,
    allow_legacy: bool = False,
) -> Dict[str, float]:
    """聚合影子持仓：优先 journal.records，其次 Ledger/文件中的 shadow_record，自动去重共用账本的重复计数。

    中文：T2-2 文件/对象路径默认先 verify_chain_with_archives（共享锁读防半写），失败抛 LedgerCorruptionError，
    不再静默聚合伪造 JSONL；历史区间须显式 allow_legacy=True 豁免旧式 hash。
    """
    out: Dict[str, float] = {}

    def add(sym: str, q: float):
        if not sym:
            return
        out[sym] = out.get(sym, 0) + float(q)

    records: List[Dict[str, Any]] = []
    # 来自内存 journal：兼容 records 属性/_records/list/dict 多形态
    if journal is not None:
        if hasattr(journal, "records"):
            try:
                records = list(journal.records)  # property
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning("journal records fallback: %s", exc)
                records = list(getattr(journal, "_records", []))
        elif hasattr(journal, "_records"):
            records = list(getattr(journal, "_records", []))
        elif isinstance(journal, list):
            records = journal  # type: ignore[assignment]
        elif isinstance(journal, dict):
            records = [journal]
        for tr in records:
            # 中文：非 dict 交易不可静默丢弃（少计影子持仓会误报 zero_diff），fail-closed
            if not isinstance(tr, dict):
                logger.warning("aggregate_shadow unsupported journal trade type %r: %r", type(tr).__name__, tr)
                raise ValueError(f"unsupported journal trade type: {type(tr).__name__}")
            sym, q = _shadow_qty_from_trade(tr)
            add(sym, q)

    def _same_file_by_inode(a: Path, b: Path) -> bool:
        """以 (st_dev, st_ino) 判同文件（P2），先 stat 再回退 resolve/absolute 对比."""
        try:
            sa = a.stat()
            sb = b.stat()
            # Windows 上 st_ino 可能为 0，回退到 resolve 对比；否则以 inode 判定
            if sa.st_ino != 0 and sb.st_ino != 0:
                return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)
            return a.resolve() == b.resolve()
        except (OSError, ValueError, RuntimeError):
            try:
                return a.resolve() == b.resolve()
            except Exception:
                return a.absolute().as_posix() == b.absolute().as_posix()

    # 预计算去重标记：journal 与 ledger 是否同源 —— P2: 以 (st_dev, st_ino) 判同文件，避免硬链接/同名不同 inode 误判
    # 中文：list/dict journal 无来源信息，与 ledger/ledger_path 同传时 fail-closed（防双计），不静默聚合
    if journal is not None and (ledger is not None or ledger_path is not None):
        if isinstance(journal, (list, dict)) or not hasattr(journal, "ledger"):
            logger.warning("aggregate_shadow: journal and ledger/ledger_path both given without provenance; refusing to aggregate both")
            raise ValueError("aggregate_shadow: journal and ledger/ledger_path both given without provenance (risk of double-count); pass only one")
    same_ledger = False
    if journal is not None and ledger is not None:
        try:
            j_ledger = getattr(journal, "ledger", None)
            if j_ledger is not None and getattr(j_ledger, "path", None) is not None and getattr(ledger, "path", None) is not None:
                same_ledger = _same_file_by_inode(Path(j_ledger.path), Path(ledger.path))  # type: ignore[union-attr]
            else:
                same_ledger = j_ledger is ledger  # 回退：无路径时仍用 identity
        except (OSError, ValueError, RuntimeError) as exc:
            logger.warning("same_ledger resolve failed: %s", exc, exc_info=True)
            same_ledger = getattr(journal, "ledger", None) is ledger
    # P2: same_file 去重与 same_ledger 统一语义，均以 inode 优先
    same_file = False
    lp: Path | None = None
    if ledger_path is not None:
        lp = Path(ledger_path)
        if journal is not None and hasattr(journal, "ledger") and getattr(journal.ledger, "path", None) is not None:
            try:
                same_file = _same_file_by_inode(Path(journal.ledger.path), lp)  # type: ignore[union-attr]
            except (OSError, ValueError, RuntimeError) as exc:
                logger.warning("same_file resolve failed: %s", exc, exc_info=True)
                same_file = False

    # 来自 Ledger 对象：解析 shadow_record/trade 与直接 symbol 记录
    # 中文：无 _read_all 的 ledger 对象不可静默忽略（会漏计而误报 zero_diff），fail-closed
    if ledger is not None and not hasattr(ledger, "_read_all"):
        logger.warning("aggregate_shadow: ledger object missing _read_all: %r", type(ledger))
        raise TypeError(f"aggregate_shadow: ledger object missing _read_all: {type(ledger)!r}")
    if ledger is not None and hasattr(ledger, "_read_all"):
        if same_ledger:
            # 已通过 journal 计数，跳过 ledger 避免双计
            pass
        else:
            try:
                # T2-2: 先 verify（对象路径经共享锁读），失败直接抛 LedgerCorruptionError，不聚合脏数据
                # 中文：仅 allow_legacy 旧签名 TypeError 回退；verify 自身抛错 LOUD 透出
                if verify and hasattr(ledger, "verify"):
                    try:
                        _ok = ledger.verify(allow_legacy=allow_legacy)
                    except TypeError as _vte:
                        if "allow_legacy" in str(_vte):
                            _ok = ledger.verify()
                        else:
                            raise
                    if not _ok:
                        from hero_quant.governance.ledger import ChainBreak, LedgerCorruptionError

                        logger.warning("aggregate_shadow ledger verify failed for %s", getattr(ledger, "path", ledger))
                        raise LedgerCorruptionError(ChainBreak(0, None, "record_hash_mismatch", f"ledger verify failed for {getattr(ledger, 'path', ledger)}"))
                entries = ledger._read_all()
                for e in entries:
                    if isinstance(e, dict) and "_raw" in e:
                        from hero_quant.governance.ledger import ChainBreak, LedgerCorruptionError

                        logger.warning("aggregate_shadow ledger corrupt line: %r", str(e.get("_raw"))[:200])
                        raise LedgerCorruptionError(ChainBreak(0, None, "malformed_json", str(e.get("_raw"))))
                    rec = e.get("record", {}) if isinstance(e, dict) else {}
                    if rec.get("action") == "shadow_record":
                        trade = rec.get("trade", {})
                        if isinstance(trade, dict):
                            sym, q = _shadow_qty_from_trade(trade)
                            add(sym, q)
                    elif "symbol" in rec and ("qty" in rec or "quantity" in rec):
                        sym, q = _shadow_qty_from_trade(rec)
                        add(sym, q)
            except Exception as exc:
                logger.warning("ledger _read_all failed: %s", exc, exc_info=exc)
                raise
    elif lp is not None:
        if lp.exists():
            try:
                # T2-2: 先 verify_chain_with_archives（共享锁读防半写），失败抛 LedgerCorruptionError，不出聚合
                # 中文：仅 allow_legacy 旧签名 TypeError 回退；verify 自身抛错 LOUD 透出
                if verify:
                    from hero_quant.governance.ledger import ChainBreak, LedgerCorruptionError, verify_chain_with_archives

                    try:
                        _vr = verify_chain_with_archives(lp, allow_legacy=allow_legacy)
                    except TypeError as _ate:
                        if "allow_legacy" in str(_ate):
                            _vr = verify_chain_with_archives(lp)
                        else:
                            raise
                    if not _vr.ok:
                        brk = _vr.first_break
                        logger.warning("aggregate_shadow ledger verify failed for %s: %s", lp, brk)
                        raise LedgerCorruptionError(
                            ChainBreak(
                                brk.index if brk else 0,
                                brk.seq if brk else None,
                                brk.reason if brk else "record_hash_mismatch",
                                brk.detail if brk else f"ledger verify failed for {lp}",
                            )
                        )
                # T2-2: 对账用共享锁读（防半写），不用裸 read_text
                from hero_quant.governance.ledger import _lock_shared as _rec_lock_shared
                from hero_quant.governance.ledger import _unlock as _rec_unlock

                with open(lp, "rb") as _h:
                    try:
                        _rec_lock_shared(_h)
                        _h.seek(0)
                        _raw = _h.read()
                    finally:
                        try:
                            _rec_unlock(_h)
                        except Exception:
                            pass
                try:
                    text = _raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    from hero_quant.governance.ledger import ChainBreak, LedgerCorruptionError

                    logger.warning("aggregate_shadow ledger decode failed for %s: %s", lp, exc)
                    raise LedgerCorruptionError(ChainBreak(0, None, "malformed_json", f"decode_error: {exc}")) from exc
                if "\x00" in text:
                    from hero_quant.governance.ledger import ChainBreak, LedgerCorruptionError

                    logger.warning("aggregate_shadow ledger NUL byte for %s", lp)
                    raise LedgerCorruptionError(ChainBreak(0, None, "malformed_json", "NUL byte in ledger"))
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except (json.JSONDecodeError, ValueError) as exc:
                        logger.warning("aggregate_shadow malformed json line %r: %s", line[:200], exc)
                        # 中文：坏 JSONL 直接 fail-closed 抛错，避免静默丢数据后误算零差额
                        raise ValueError(f"malformed ledger line: {line[:200]!r}") from exc
                    rec = e.get("record", {}) if isinstance(e, dict) else {}
                    # P2: 统一去重口径 —— same_file 与 same_ledger 均视为同源，已由 journal 计数则跳过，避免按分支分别 continue 导致一支漏判而双计
                    if same_file and rec.get("action") == "shadow_record":
                        continue
                    if same_file and "symbol" in rec and ("qty" in rec or "quantity" in rec):
                        continue
                    if rec.get("action") == "shadow_record":
                        trade = rec.get("trade", {})
                        if isinstance(trade, dict):
                            sym, q = _shadow_qty_from_trade(trade)
                            add(sym, q)
                    elif "symbol" in rec and ("qty" in rec or "quantity" in rec):
                        sym, q = _shadow_qty_from_trade(rec)
                        add(sym, q)
                    elif rec:
                        # 非持仓记录也不静默：调试可见
                        logger.debug("aggregate_shadow skip non-holding record %r", rec.get("action"))
            except Exception as exc:
                logger.warning("ledger_path read failed: %s", exc, exc_info=exc)
                raise

    # 归一化：消除 -0.0 并保留净持仓符号
    cleaned: Dict[str, float] = {}
    for k, v in out.items():
        fv = float(v)
        if abs(fv) < 1e-9:
            fv = 0.0
        cleaned[k] = fv
    return cleaned


def reconcile(
    shadow: Dict[str, float],
    broker: Dict[str, float],
    tolerance: float = 1e-6,
) -> ReconcileResult:
    """逐 symbol 比对影子与券商持仓，容差内视为零差额，total_diff 为绝对差之和。"""
    # P2: missing validation - tolerance must be non-negative float
    try:
        tolerance = float(tolerance)
    except (ValueError, TypeError) as e:
        logger.warning("reconcile invalid tolerance %r: %s", tolerance, e)
        raise ValueError(f"tolerance must be numeric, got {tolerance!r}") from e
    if tolerance < 0:
        logger.warning("reconcile tolerance negative %r", tolerance)
        raise ValueError(f"tolerance must be >=0, got {tolerance}")
    if not isinstance(shadow, dict) or not isinstance(broker, dict):
        logger.warning("reconcile shadow/broker must be dict, got %r / %r", type(shadow), type(broker))
        raise TypeError("shadow and broker must be dict")
    all_syms = set(shadow.keys()) | set(broker.keys())
    diffs: List[Dict[str, Any]] = []
    total = 0.0
    for sym in sorted(all_syms):
        try:
            s = float(shadow.get(sym, 0))
        except (ValueError, TypeError) as exc:
            logger.warning("reconcile non-numeric shadow holding for %r: %s", sym, exc)
            raise ValueError(f"non-numeric shadow holding for {sym!r}") from exc
        try:
            b = float(broker.get(sym, 0))
        except (ValueError, TypeError) as exc:
            logger.warning("reconcile non-numeric broker holding for %r: %s", sym, exc)
            raise ValueError(f"non-numeric broker holding for {sym!r}") from exc
        # 中文：非有限持仓必须 fail-closed（NaN 会使 ad > tolerance 恒 False 而误报 zero_diff）
        if not math.isfinite(s) or not math.isfinite(b):
            logger.warning("reconcile non-finite holding for %r: shadow=%r broker=%r", sym, s, b)
            raise ValueError(f"non-finite holding for {sym!r}: shadow={s!r} broker={b!r}")
        d = s - b
        ad = abs(d)
        # 修复 tolerance vs total_diff 不一致：仅容差外的差额计入 total，保持 zero 与 total 一致
        if ad > tolerance:
            diffs.append({"symbol": sym, "shadow": s, "broker": b, "diff": d, "abs_diff": ad})
            total += ad
        else:
            # 容差内视为 0，不计入 total，避免 zero=True 却 total>0 的矛盾
            pass
    zero = len(diffs) == 0
    # round total for stable output
    total = round(total, 10)
    return ReconcileResult(shadow=dict(shadow), positions=dict(broker), diffs=diffs, zero_diff=zero, total_diff=total)


def reconcile_files(
    ledger_path: str | Path,
    positions_csv: str | Path,
    tolerance: float = 1e-6,
    journal: Any | None = None,
    wall_time_budget: float | None = None,
    *,
    allow_legacy: bool = False,
) -> ReconcileResult:
    """文件级对账：ledger.jsonl（或 journal） vs positions.csv，超时受 wall-time budget 约束。

    中文：T2-2 先 verify_chain_with_archives（含归档，共享锁读），失败抛 LedgerCorruptionError，
    不出 zero_diff 聚合结果；历史区间须显式 allow_legacy=True 豁免。
    """
    import time as _t

    _start = _t.monotonic()
    _status = "success"
    try:
        # wall-time budget enforcement (governance)
        _budget = wall_time_budget
        if _budget is None:
            import os as _os

            raw = _os.environ.get("HERO_WALL_TIME_BUDGET", _os.environ.get("HERO_WALL_TIME_BUDGET_SECONDS", "")).strip()
            if raw:
                try:
                    _budget = float(raw)
                except Exception:
                    _budget = None
        broker = load_positions_csv(positions_csv)
        # T2-2: 先 verify（含归档全历史），失败直接抛 LedgerCorruptionError，不出 zero_diff
        # 中文：verify 自身抛错（锁/IO/替身 verify 抛错）LOUD 透出；仅 TypeError 做旧签名回退
        if journal is None:
            from hero_quant.governance.ledger import ChainBreak as _CB
            from hero_quant.governance.ledger import LedgerCorruptionError as _LCE
            from hero_quant.governance.ledger import verify_chain_with_archives as _vca

            try:
                _vr = _vca(Path(ledger_path), allow_legacy=allow_legacy)
            except TypeError as _te:
                # 仅旧签名无 allow_legacy 形参时回退；其他 TypeError（如替身内部错误）不吞
                if "allow_legacy" in str(_te):
                    _vr = _vca(Path(ledger_path))
                else:
                    raise
            if not _vr.ok:
                _brk = _vr.first_break
                logger.warning("reconcile_files ledger verify failed for %s: %s", ledger_path, _brk)
                raise _LCE(
                    _CB(
                        _brk.index if _brk else 0,
                        _brk.seq if _brk else None,
                        _brk.reason if _brk else "record_hash_mismatch",
                        _brk.detail if _brk else f"ledger verify failed for {ledger_path}",
                    )
                )
            shadow = aggregate_shadow(journal=journal, ledger_path=ledger_path, allow_legacy=allow_legacy)
        else:
            shadow = aggregate_shadow(journal=journal, ledger_path=None, allow_legacy=allow_legacy)
        res = reconcile(shadow, broker, tolerance=tolerance)
        # check budget after work
        if _budget is not None and _budget > 0:
            _elapsed = _t.monotonic() - _start
            if _elapsed > float(_budget):
                _status = "exceeded"
                try:
                    from hero_quant.metrics import inc_wall_time_exceeded

                    inc_wall_time_exceeded("reconcile")
                except Exception as _exc:
                    logger.warning("silent handled: governance: reconcile wall-time observe best-effort", exc_info=_exc)  # intentional: governance: reconcile wall-time observe best-effort
                    pass  # intentional governance: reconcile wall-time observe best-effort
                from hero_quant.governance.wall_time import WallTimeExceeded

                raise WallTimeExceeded("reconcile", float(_budget), float(_elapsed))
        return res
    except Exception:
        if _status != "exceeded":
            _status = "error"
        raise
    finally:
        try:
            _elapsed = _t.monotonic() - _start
            from hero_quant.metrics import observe_wall_time

            observe_wall_time("reconcile", float(_elapsed), status=_status)
        except Exception as _exc:
            logger.warning("silent handled: governance: reconcile wall-time observe best-effort", exc_info=_exc)  # intentional: governance: reconcile wall-time observe best-effort
            pass  # intentional governance: reconcile wall-time observe best-effort


def daily_reconciliation(
    date: str,
    ledger_path: str | Path,
    positions_csv: str | Path,
    tolerance: float = 1e-6,
    journal: Any | None = None,
    wall_time_budget: float | None = None,
    *,
    allow_legacy: bool = False,
) -> Dict[str, Any]:
    """日终对账作业：返回含 date/zero_diff/diffs/verified 的报告，并校验账本完整性。

    中文：T2-2 先 verify_chain_with_archives（含归档），失败抛 LedgerCorruptionError，
    不再返回 verified=False + zero_diff=True 的误导报告；历史区间须显式 allow_legacy=True 豁免。
    """
    import time as _t

    _start = _t.monotonic()
    _status = "success"
    result: ReconcileResult | None = None
    try:
        # T2-2: 先 verify（含归档全历史，共享锁读），失败直接抛 LedgerCorruptionError，不出 zero_diff 报告
        # 中文：verify 自身抛错（锁/IO/替身 verify 抛错）必须 LOUD 透出，不吞成 ok/false
        # 中文：仅 allow_legacy 旧签名 TypeError 做回退；其他 TypeError（如替身内部错误）不吞
        if journal is None:
            from hero_quant.governance.ledger import ChainBreak as _DCB
            from hero_quant.governance.ledger import LedgerCorruptionError as _DLCE
            from hero_quant.governance.ledger import verify_chain_with_archives as _dvca

            try:
                _dvr = _dvca(Path(ledger_path), allow_legacy=allow_legacy)
            except TypeError as _dte:
                if "allow_legacy" in str(_dte):
                    _dvr = _dvca(Path(ledger_path))
                else:
                    raise
            if not _dvr.ok:
                _dbrk = _dvr.first_break
                logger.warning("daily_reconciliation ledger verify failed for %s: %s", ledger_path, _dbrk)
                raise _DLCE(
                    _DCB(
                        _dbrk.index if _dbrk else 0,
                        _dbrk.seq if _dbrk else None,
                        _dbrk.reason if _dbrk else "record_hash_mismatch",
                        _dbrk.detail if _dbrk else f"ledger verify failed for {ledger_path}",
                    )
                )
        # 单次 budget：不在此处双重委托给 reconcile_files，避免双 observe/双计数
        result = reconcile_files(ledger_path, positions_csv, tolerance=tolerance, journal=journal, wall_time_budget=None, allow_legacy=allow_legacy)
        # check budget once here
        _budget = wall_time_budget
        if _budget is None:
            import os as _os2

            raw = _os2.environ.get("HERO_WALL_TIME_BUDGET", _os2.environ.get("HERO_WALL_TIME_BUDGET_SECONDS", "")).strip()
            if raw:
                try:
                    _budget = float(raw)
                except (ValueError, TypeError):
                    _budget = None
        if _budget is not None and _budget > 0:
            _elapsed = _t.monotonic() - _start
            if _elapsed > float(_budget):
                _status = "exceeded"
                try:
                    from hero_quant.metrics import inc_wall_time_exceeded

                    inc_wall_time_exceeded("daily_reconciliation")
                except Exception as exc:
                    logger.warning("inc_wall_time_exceeded failed: %s", exc)
                from hero_quant.governance.wall_time import WallTimeExceeded

                raise WallTimeExceeded("daily_reconciliation", float(_budget), float(_elapsed))
    except Exception:
        if _status != "exceeded":
            _status = "error"
        raise
    finally:
        try:
            _elapsed = _t.monotonic() - _start
            from hero_quant.metrics import observe_wall_time

            observe_wall_time("daily_reconciliation", float(_elapsed), status=_status)
        except Exception as exc:
            logger.warning("observe_wall_time failed: %s", exc)
    if result is None:
        raise RuntimeError("daily_reconciliation: missing result")
    # T2-2: verify 已在入口先行（失败已抛 LedgerCorruptionError）；journal=None 路径 verified=True（已验过）。
    # 中文：保持 verified=False 语义（verify 异常置 False 并 warning，避免与 skip 混淆），供 journal/替身 Ledger 路径兼容。
    verified: bool | None = True
    if journal is not None:
        try:
            from hero_quant.governance.ledger import Ledger as _DLedger

            _ledger_obj = _DLedger(Path(ledger_path))
            try:
                _vok = _ledger_obj.verify(allow_legacy=allow_legacy)
            except TypeError as _vte:
                if "allow_legacy" in str(_vte):
                    _vok = _ledger_obj.verify()
                else:
                    raise
            if not _vok:
                verified = False
                logger.warning("daily_reconciliation ledger re-verify failed for %s", ledger_path)
                from hero_quant.governance.ledger import ChainBreak as _JCB
                from hero_quant.governance.ledger import LedgerCorruptionError as _JLCE

                raise _JLCE(_JCB(0, None, "record_hash_mismatch", f"ledger verify failed for {ledger_path}"))
            verified = True
        except Exception as exc:
            from hero_quant.governance.ledger import LedgerCorruptionError as _JLCE2

            if isinstance(exc, _JLCE2):
                raise
            logger.warning("ledger verify failed for %s: %s", ledger_path, exc, exc_info=exc)
            verified = False
            raise

    report: Dict[str, Any] = {
        "date": date,
        "ledger_path": str(ledger_path),
        "positions_csv": str(positions_csv),
        "shadow": result.shadow,
        "positions": result.positions,
        "diffs": result.diffs,
        "zero_diff": result.zero_diff,
        "total_diff": result.total_diff,
        "verified": verified,
        "tolerance": tolerance,
    }
    return report
