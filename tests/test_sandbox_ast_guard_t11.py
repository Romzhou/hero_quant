"""T1-1 沙箱AST守卫回归：attrgetter/itemgetter/methodcaller + dunder字符串参数 + from-import原名/别名双校验。"""
import pytest

from hero_quant.sandbox.ast_guard import SandboxViolation, check_source


def test_t11_operator_attrgetter_blocked():
    with pytest.raises(SandboxViolation):
        check_source('import operator; f = operator.attrgetter("__class__")')
    with pytest.raises(SandboxViolation):
        check_source('import operator as op; f = op.attrgetter("__subclasses__")')
    with pytest.raises(SandboxViolation):
        check_source('from operator import attrgetter; attrgetter("__dict__")')
    with pytest.raises(SandboxViolation):
        check_source('from operator import attrgetter as ag; ag("__class__")')


def test_t11_operator_itemgetter_methodcaller_blocked():
    with pytest.raises(SandboxViolation):
        check_source('import operator; f = operator.itemgetter("__class__")')
    with pytest.raises(SandboxViolation):
        check_source('import operator; f = operator.methodcaller("__class__")')


def test_t11_dunder_string_arg_blocked_for_any_call():
    # 通用纵深：任何 Call 的字符串常量参数命中 BANNED_DUNDER_ATTRS 即拒
    with pytest.raises(SandboxViolation):
        check_source('import operator; f = operator.attrgetter("__mro__")')
    with pytest.raises(SandboxViolation):
        check_source('from pandas import read_pickle as r; r("__class__")')


def test_t11_fromimport_original_and_alias_both_checked():
    with pytest.raises(SandboxViolation):
        check_source('from pandas import read_pickle as r; r("x.pkl")')
    with pytest.raises(SandboxViolation):
        check_source('from pandas import read_pickle; read_pickle("x.pkl")')
    with pytest.raises(SandboxViolation):
        check_source('from yaml import unsafe_load as u; u("x")')
    #  benign from-import 不误杀
    check_source('from pandas import DataFrame; x = DataFrame({"a": [1]})')


def test_t11_legit_quant_code_still_passes():
    check_source('import pandas as pd; x = pd.DataFrame({"a": [1,2,3]}); y = x["a"].sum()')
