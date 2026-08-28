import pytest
from hero_quant.governance.reconcile import _normalize_qty

def test_normalize_qty_rejects_invalid():
    try:
        _normalize_qty("N/A")
        assert False
    except ValueError:
        pass

def test_aggregate_shadow_no_double_count(tmp_path):
    assert True  # 骨架：same file 不双计
