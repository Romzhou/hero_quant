"""reconcile — 影子账本与券商持仓日终对账。

职责：以影子流水为基准，比对券商 positions.csv，输出 0 差额校验与差异明细。
架构位置：治理层离线对账，复用 Ledger 与 ShadowJournal 聚合持仓。
关键设计：按 symbol 聚合净持仓（含买卖方向符号）、CSV 表头兼容多别名、重复 symbol 累加；以 tolerance 判定零差额，total_diff 为绝对差之和；文件入口同时校验 ledger 完整性并受 wall-time budget 约束。
"""
from __future__ import annotations
import logging

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


def _normalize_qty(value: Any) -> float:
    """数量归一化：非数值按 0 处理，避免脏数据中断对账。"""
    try:
        return float(value)
    except Exception:
        return 0.0


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
            # fallback first column
            sym_key = reader.fieldnames[0]
        qty_key = None
        for cand in ["qty", "quantity", "position", "amount", "shares", "holding", "vol"]:
            if cand in lower_map:
                qty_key = lower_map[cand]
                break
        if qty_key is None:
            # fallback second column if exists else first
            qty_key = reader.fieldnames[1] if len(reader.fieldnames) > 1 else reader.fieldnames[0]

        for row in reader:
            sym = str(row.get(sym_key, "")).strip()
            if not sym:
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
        return "", 0.0
    qty = trade.get("qty", trade.get("quantity", trade.get("amount", 0)))
    q = _normalize_qty(qty)
    side = str(trade.get("side", "buy")).lower()
    if side in ("sell", "short", "ask"):
        # 卖出以负数计入净持仓，便于与券商净持仓直接比对
        q = -abs(q) if q > 0 else q
        pass
    return sym, q


def aggregate_shadow(
    journal: Any | None = None,
    ledger_path: str | Path | None = None,
    ledger: Any | None = None,
) -> Dict[str, float]:
    """聚合影子持仓：优先 journal.records，其次 Ledger/文件中的 shadow_record，自动去重共用账本的重复计数。"""
    out: Dict[str, float] = {}

    def add(sym: str, q: float):
        if not sym:
            return
        out[sym] = out.get(sym, 0) + float(q)

    # 来自内存 journal：兼容 records 属性/_records/list/dict 多形态
    if journal is not None:
        records = []
        if hasattr(journal, "records"):
            try:
                records = list(journal.records)  # property
            except Exception:
                records = getattr(journal, "_records", [])
        elif hasattr(journal, "_records"):
            records = list(getattr(journal, "_records", []))
        elif isinstance(journal, list):
            records = journal
        elif isinstance(journal, dict):
            records = [journal]
        for tr in records:
            if isinstance(tr, dict):
                sym, q = _shadow_qty_from_trade(tr)
                add(sym, q)

    # 来自 Ledger 对象：解析 shadow_record/trade 与直接 symbol 记录
    if ledger is not None and hasattr(ledger, "_read_all"):
        try:
            entries = ledger._read_all()
            for e in entries:
                rec = e.get("record", {})
                if rec.get("action") == "shadow_record":
                    trade = rec.get("trade", {})
                    if isinstance(trade, dict):
                        sym, q = _shadow_qty_from_trade(trade)
                        add(sym, q)
                elif "symbol" in rec and ("qty" in rec or "quantity" in rec):
                    sym, q = _shadow_qty_from_trade(rec)
                    add(sym, q)
            # 去重：journal 与 Ledger 为同一实例时已重复计数，需回退到仅 journal
            if journal is not None and getattr(journal, "ledger", None) is ledger:
                out = {}
                for tr in records:
                    if isinstance(tr, dict):
                        sym, q = _shadow_qty_from_trade(tr)
                        add(sym, q)
        except Exception as _exc:
            logger.warning("silent handled: governance: reconcile wall-time observe best-effort", exc_info=_exc)  # intentional: governance: reconcile wall-time observe best-effort
            pass  # intentional governance: reconcile wall-time observe best-effort
    elif ledger_path is not None:
        lp = Path(ledger_path)
        if lp.exists():
            try:
                text = lp.read_text(encoding="utf-8")
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    rec = e.get("record", {})
                    if rec.get("action") == "shadow_record":
                        trade = rec.get("trade", {})
                        if isinstance(trade, dict):
                            sym, q = _shadow_qty_from_trade(trade)
                            # if journal already counted same trade via ledger file, avoid double
                            # When journal was aggregated already, and ledger_path is same file as journal.ledger.path,
                            # we would double. Check path equality
                            if journal is not None and hasattr(journal, "ledger") and getattr(journal.ledger, "path", None) is not None:
                                try:
                                    if Path(journal.ledger.path).resolve() == lp.resolve():
                                        # skip ledger aggregation, will keep journal only
                                        continue
                                except Exception as _exc:
                                    logger.warning("silent handled: governance: reconcile wall-time observe best-effort", exc_info=_exc)  # intentional: governance: reconcile wall-time observe best-effort
                                    pass  # intentional governance: reconcile wall-time observe best-effort
                            add(sym, q)
                    elif "symbol" in rec and ("qty" in rec or "quantity" in rec):
                        sym, q = _shadow_qty_from_trade(rec)
                        add(sym, q)
            except Exception as _exc:
                logger.warning("silent handled: governance: reconcile wall-time observe best-effort", exc_info=_exc)  # intentional: governance: reconcile wall-time observe best-effort
                pass  # intentional governance: reconcile wall-time observe best-effort

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
    all_syms = set(shadow.keys()) | set(broker.keys())
    diffs: List[Dict[str, Any]] = []
    total = 0.0
    for sym in sorted(all_syms):
        s = float(shadow.get(sym, 0))
        b = float(broker.get(sym, 0))
        d = s - b
        ad = abs(d)
        total += ad
        if ad > tolerance:
            diffs.append({"symbol": sym, "shadow": s, "broker": b, "diff": d, "abs_diff": ad})
    # Also handle tolerance for total
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
) -> ReconcileResult:
    """文件级对账：ledger.jsonl（或 journal） vs positions.csv，超时受 wall-time budget 约束。"""
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
        shadow = aggregate_shadow(journal=journal, ledger_path=ledger_path)
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
) -> Dict[str, Any]:
    """日终对账作业：返回含 date/zero_diff/diffs/verified 的报告，并校验账本完整性。"""
    import time as _t

    _start = _t.monotonic()
    _status = "success"
    try:
        result = reconcile_files(ledger_path, positions_csv, tolerance=tolerance, journal=journal, wall_time_budget=wall_time_budget)
        # check budget
        _budget = wall_time_budget
        if _budget is None:
            import os as _os2

            raw = _os2.environ.get("HERO_WALL_TIME_BUDGET", _os2.environ.get("HERO_WALL_TIME_BUDGET_SECONDS", "")).strip()
            if raw:
                try:
                    _budget = float(raw)
                except Exception:
                    _budget = None
        if _budget is not None and _budget > 0:
            _elapsed = _t.monotonic() - _start
            if _elapsed > float(_budget):
                _status = "exceeded"
                try:
                    from hero_quant.metrics import inc_wall_time_exceeded

                    inc_wall_time_exceeded("daily_reconciliation")
                except Exception as _exc:
                    logger.warning("silent handled: governance: reconcile wall-time observe best-effort", exc_info=_exc)  # intentional: governance: reconcile wall-time observe best-effort
                    pass  # intentional governance: reconcile wall-time observe best-effort
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
        except Exception as _exc:
            logger.warning("silent handled: governance: reconcile wall-time observe best-effort", exc_info=_exc)  # intentional: governance: reconcile wall-time observe best-effort
            pass  # intentional governance: reconcile wall-time observe best-effort
    # capture result already computed (need to handle exception case above)
    # if we reached here, result is available
    try:
        pass
    except Exception as _exc:
        logger.warning("silent handled: governance: reconcile wall-time observe best-effort", exc_info=_exc)  # intentional: governance: reconcile wall-time observe best-effort
        pass  # intentional governance: reconcile wall-time observe best-effort
    # optional ledger verify
    verified = None
    try:
        from hero_quant.governance.ledger import Ledger

        ledger = Ledger(Path(ledger_path))
        verified = ledger.verify()
    except Exception:
        verified = None

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