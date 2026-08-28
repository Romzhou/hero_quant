"""Task19 TDD red: daily shadow ledger vs positions reconciliation 0差额.

Keep minimal daily reconciliation, use existing ledger and shadow journal.
"""
import csv
import json
import tempfile
from pathlib import Path


def test_reconcile_zero_diff():
    """Shadow positions vs broker CSV 0差额 should pass."""
    from hero_quant.governance.reconcile import reconcile, load_positions_csv, aggregate_shadow


def test_reconcile_qty_validation_raises(tmp_path):
    """P2: missing validation - invalid qty should raise ValueError with log, not silent."""
    from hero_quant.governance.reconcile import _normalize_qty, load_positions_csv
    import pytest, csv
    from pathlib import Path
    with pytest.raises(ValueError):
        _normalize_qty(None)
    with pytest.raises(ValueError):
        _normalize_qty("")
    with pytest.raises(ValueError):
        _normalize_qty("   ")
    with pytest.raises(ValueError):
        _normalize_qty("not_a_number")
    # CSV with invalid qty should propagate error
    p = Path(tmp_path) / "bad.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "qty"])
        w.writerow(["600519.SH", "bad_qty"])
    with pytest.raises(ValueError):
        load_positions_csv(p)


def test_reconcile_tolerance_validation():
    """P2: tolerance must be non-negative, otherwise ValueError."""
    from hero_quant.governance.reconcile import reconcile
    import pytest
    with pytest.raises(ValueError):
        reconcile({"A": 1}, {"A": 1}, tolerance=-1)
    with pytest.raises(ValueError):
        reconcile({"A": 1}, {"A": 1}, tolerance="bad")  # type: ignore


def test_aggregate_shadow_deterministic(tmp_path):
    """P2: non-deterministic ordering - reconcile sorted keys deterministically."""
    from hero_quant.governance.reconcile import reconcile
    s1 = {"B": 100, "A": 50}
    b1 = {"A": 50, "B": 100}
    r1 = reconcile(s1, b1)
    r2 = reconcile({"A": 50, "B": 100}, {"B": 100, "A": 50})
    assert r1.diffs == r2.diffs
    assert r1.zero_diff == r2.zero_diff
    assert r1.total_diff == r2.total_diff


def test_reconcile_zero_diff():
    """Shadow positions vs broker CSV 0差额 should pass."""
    from hero_quant.governance.reconcile import reconcile, load_positions_csv, aggregate_shadow

    # shadow journal with trades
    from hero_quant.shadow import ShadowJournal

    j = ShadowJournal()
    j.record({"symbol": "600519.SH", "qty": 100, "price": 10, "side": "buy"})
    j.record({"symbol": "000001.SZ", "qty": 200, "price": 8, "side": "buy"})
    shadow = aggregate_shadow(journal=j)

    # broker positions.csv matching shadow
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "positions.csv"
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "qty"])
            w.writerow(["600519.SH", 100])
            w.writerow(["000001.SZ", 200])
        broker = load_positions_csv(p)
        result = reconcile(shadow, broker)
        assert result.zero_diff is True, f"expected 0 diff got {result.diffs}"
        assert result.total_diff == 0


def test_reconcile_detects_diff():
    from hero_quant.governance.reconcile import reconcile

    shadow = {"600519.SH": 100, "000001.SZ": 200}
    broker = {"600519.SH": 100, "000001.SZ": 190}
    result = reconcile(shadow, broker)
    assert result.zero_diff is False
    assert len(result.diffs) >= 1
    # diff entry for 000001.SZ should be 10
    diff_map = {d["symbol"]: d for d in result.diffs}
    assert diff_map["000001.SZ"]["diff"] == 10


def test_reconcile_files_with_ledger(tmp_path):
    """reconcile via ledger jsonl file vs positions.csv."""
    from hero_quant.governance.ledger import Ledger
    from hero_quant.shadow import ShadowJournal
    from hero_quant.governance.reconcile import reconcile_files, ReconcileResult

    ledger_path = tmp_path / "shadow_ledger.jsonl"
    ledger = Ledger(ledger_path)
    j = ShadowJournal(ledger=ledger)
    j.record({"symbol": "600519.SH", "qty": 150, "price": 12, "side": "buy", "pnl": 0.5})
    j.record({"symbol": "BTC/USDT", "qty": 2, "price": 30000, "side": "buy", "pnl": 0.1})

    # also test ledger aggregation path
    positions_path = tmp_path / "positions.csv"
    with positions_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "quantity"])
        w.writerow(["600519.SH", 150])
        w.writerow(["BTC/USDT", 2])

    result = reconcile_files(ledger_path, positions_path)
    assert isinstance(result, ReconcileResult)
    assert result.zero_diff is True


def test_daily_reconciliation_report(tmp_path):
    from hero_quant.governance.ledger import Ledger
    from hero_quant.shadow import ShadowJournal
    from hero_quant.governance.reconcile import daily_reconciliation

    ledger_path = tmp_path / "ledger.jsonl"
    ledger = Ledger(ledger_path)
    j = ShadowJournal(ledger=ledger)
    j.record({"symbol": "600519.SH", "qty": 100, "price": 10, "side": "buy"})

    positions_path = tmp_path / "positions.csv"
    with positions_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "qty"])
        w.writerow(["600519.SH", 100])

    report = daily_reconciliation(date="2026-08-21", ledger_path=ledger_path, positions_csv=positions_path)
    assert report["date"] == "2026-08-21"
    assert report["zero_diff"] is True
    assert "diffs" in report
    assert report["total_diff"] == 0
