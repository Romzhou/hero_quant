"""A3b lane TDD：脱敏/审计/审批 20 条 failing test。

覆盖：middleware/audit.py×3、middleware/pii.py×5、security/approval.py×6、
security/redaction.py×5、tools/redaction.py×1。
先跑红，再修源码，再跑绿。
"""

from __future__ import annotations

import os
import pathlib
from unittest import mock

import pytest


# ---------- audit.py ×3 ----------


def test_a01_audit_failure_sanitizes_error():
    """G1#1：log_tool_failure 不得存原文error（截断+去换行+hash）。"""
    from hero_quant.api.middleware import audit as audit_mod

    evil = "secret_token=sk-abc\n第二行注入\r\n" + "x" * 2000
    with mock.patch.object(audit_mod.audit_logger, "warning") as w:
        audit_mod.AuditLogger.log_tool_failure(1, "t", evil)
    assert w.call_count == 1
    extra = w.call_args.kwargs.get("extra", w.call_args.args[1] if len(w.call_args.args) > 1 else {})
    err = extra["error"]
    assert "\n" not in err and "\r" not in err  # 去换行
    assert len(err) <= 500  # 截断
    assert "第二行注入" not in err or len(err) <= 500
    assert extra.get("error_hash") and len(extra["error_hash"]) == 16  # 可取证hash
    assert evil not in err  # 非原文


def test_a02_audit_hash_covers_values():
    """G1#2：_hash_args 必须区分同类型不同值。"""
    from hero_quant.api.middleware.audit import _hash_args

    assert _hash_args({"q": "a"}) != _hash_args({"q": "entire DB dump"})


def test_a03_audit_log_call_tolerates_non_dict_args():
    """G1#3：log_tool_call 遇非dict args 不得抛错（审计永不抛）。"""
    from hero_quant.api.middleware import audit as audit_mod

    with mock.patch.object(audit_mod.audit_logger, "info"):
        audit_mod.AuditLogger.log_tool_call(1, "t", ["not", "dict"], True)  # 不得raise
        audit_mod.AuditLogger.log_tool_call(1, "t", "str-args", True)
        audit_mod.AuditLogger.log_tool_call(1, "t", None, True)


# ---------- pii.py ×5 ----------


def _reset_pii(pii_mod):
    pii_mod._PII_KEY = None
    if hasattr(pii_mod, "_WARNED_NO_KEY"):
        pii_mod._WARNED_NO_KEY = False


def test_a04_pii_encrypt_fail_closed_without_key():
    """G1#4 critical：无key时加密必须抛错，不得存 !NOENC! 明文。"""
    from hero_quant.api.middleware import pii as pii_mod

    old_env = os.environ.get("PII_ENCRYPTION_KEY")
    old_key = pii_mod._PII_KEY
    os.environ.pop("PII_ENCRYPTION_KEY", None)
    _reset_pii(pii_mod)
    try:
        with pytest.raises(RuntimeError):
            pii_mod.pii_encrypt("13800000000")
    finally:
        if old_env is not None:
            os.environ["PII_ENCRYPTION_KEY"] = old_env
        pii_mod._PII_KEY = old_key


def test_a05_pii_decrypt_fail_closed():
    """G1#5：无key/错key解密必须抛错，不得把密文当明文返回。"""
    from cryptography.fernet import Fernet

    from hero_quant.api.middleware import pii as pii_mod

    old_env = os.environ.get("PII_ENCRYPTION_KEY")
    old_key = pii_mod._PII_KEY
    try:
        os.environ["PII_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        _reset_pii(pii_mod)
        enc = pii_mod.pii_encrypt("13800000000")
        # 无key解密抛错
        os.environ.pop("PII_ENCRYPTION_KEY", None)
        _reset_pii(pii_mod)
        with pytest.raises(RuntimeError):
            pii_mod.pii_decrypt(enc)
        # 错key解密抛错（非原文返回）
        os.environ["PII_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        _reset_pii(pii_mod)
        with pytest.raises(Exception):
            out = pii_mod.pii_decrypt(enc)
            assert out == enc, "must raise, not return ciphertext"
    finally:
        if old_env is not None:
            os.environ["PII_ENCRYPTION_KEY"] = old_env
        else:
            os.environ.pop("PII_ENCRYPTION_KEY", None)
        pii_mod._PII_KEY = old_key


def test_a06_pii_keywords_cover_common_fields():
    """G1#6：关键词覆盖 name/address/mobile/bank/passport/passwd/private 等。"""
    from hero_quant.api.middleware.pii import is_pii_field

    for f in ("mobile", "user_name", "home_address", "bank_account", "credit_card_no",
              "passport_no", "birthday", "my_passwd", "private_key_path", "auth_token"):
        assert is_pii_field(f), f
    # 计量键不误杀
    assert not is_pii_field("input_tokens")


def test_a07_safe_log_args_redacts_by_default():
    """G1#7：未知键长字符串默认脱敏，不原文打日志；去换行截断。"""
    from hero_quant.api.middleware.pii import safe_log_args

    out = safe_log_args({"nickname": "张三13800000000", "note": "a" * 100, "evil": "x\ny"})
    assert out["nickname"] != "张三13800000000"
    assert out["note"] != "a" * 100 and "****" in out["note"] or "*" in out["note"]
    assert "\n" not in out["evil"]
    # 非dict容错
    assert safe_log_args(None) == {}
    assert safe_log_args(["x"]) == {}


def test_a08_pii_missing_key_warns_once():
    """G1#8：缺key warning 只打一次，不刷屏。"""
    from hero_quant.api.middleware import pii as pii_mod

    old_env = os.environ.get("PII_ENCRYPTION_KEY")
    old_key = pii_mod._PII_KEY
    os.environ.pop("PII_ENCRYPTION_KEY", None)
    _reset_pii(pii_mod)
    try:
        with mock.patch.object(pii_mod, "logger") as lg:
            pii_mod._get_key()
            pii_mod._get_key()
            assert lg.warning.call_count == 1
    finally:
        if old_env is not None:
            os.environ["PII_ENCRYPTION_KEY"] = old_env
        pii_mod._PII_KEY = old_key
        if hasattr(pii_mod, "_WARNED_NO_KEY"):
            pii_mod._WARNED_NO_KEY = False


# ---------- approval.py ×6 ----------


def test_a09_requires_approval_fail_closed():
    """G3#75：未知/None/非法策略默认需要审批（拒绝 fail-open）。"""
    from hero_quant.security.approval import requires_approval

    assert requires_approval("bogus") is True
    assert requires_approval(None) is True
    assert requires_approval("") is True
    assert requires_approval("ask") is True
    assert requires_approval("never") is False
    assert requires_approval("auto") is False


def test_a10_post_init_keeps_non_str_mode():
    """G3#76：ApprovalPolicy('never') 实例不得被丢成 ask。"""
    from hero_quant.security.approval import ApprovalPolicy, ApprovalService

    assert ApprovalService(mode=ApprovalPolicy("never")).mode == "never"
    assert ApprovalService(mode=ApprovalPolicy("auto")).mode == "auto"
    assert ApprovalService(mode="never").mode == "never"


def test_a11_request_sync_uniform_shape():
    """G3#77：三档返回统一 dict 形态（含status），且兼容旧 =="rejected" 比较。"""
    from hero_quant.security.approval import ApprovalService

    r_never = ApprovalService(mode="never").request_sync(tool="t")
    r_ask = ApprovalService(mode="ask").request_sync(tool="t")
    r_auto = ApprovalService(mode="auto").request_sync(tool="t")
    for r in (r_never, r_ask, r_auto):
        assert isinstance(r, dict), type(r)
        assert r.get("status") in ("rejected", "pending", "approved")
    assert r_never["status"] == "rejected" and r_never == "rejected"
    assert r_ask["status"] == "pending"
    assert r_auto["status"] == "approved" and r_auto == "approved"


def test_a12_no_duplicate_history_branch():
    """G3#78：删除 approval/asked|approval/decided 重复分支（泛型循环已覆盖）。

    泛型键解析仍须兼容历史事件类型携带的 policy。
    """
    from hero_quant.security import approval as ap_mod

    src = pathlib.Path("src/hero_quant/security/approval.py").read_text(encoding="utf-8")
    assert "approval/asked" not in src and "approval/decided" not in src
    assert ap_mod.effectiveApprovalPolicy([{"type": "approval/asked", "policy": "never"}]) == "never"


def test_a13_no_unreachable_ask_auto_guard():
    """G3#79：删除被 rank 钳制覆盖的不可达 ask->auto guard。"""
    src = pathlib.Path("src/hero_quant/security/approval.py").read_text(encoding="utf-8")
    assert "self.mode == ApprovalPolicy.ASK and folded == ApprovalPolicy.AUTO" not in src
    # 钳制语义保留：不可信事件不得把 ask 放宽为 auto
    from hero_quant.security.approval import ApprovalService

    assert ApprovalService(mode="ask").effective_policy([{"policy": "auto"}]) == "ask"


def test_a14_policy_hashable():
    """G3#80：ApprovalPolicy 可hash，未知类型比较返回 NotImplemented 语义（False）。"""
    from hero_quant.security.approval import ApprovalPolicy

    p = ApprovalPolicy("ask")
    assert hash(p) == hash("ask")
    assert len({p, ApprovalPolicy("ask"), ApprovalPolicy("never")}) == 2
    assert (p == 123) is False
    assert repr(p)


# ---------- security/redaction.py ×5 ----------


def test_a15_sensitive_substrings():
    """G3#90：my_passwd/my_private_key/privateKey 命中子串脱敏。"""
    from hero_quant.security.redaction import _is_sensitive_key, redact_payload

    assert _is_sensitive_key("my_passwd")
    assert _is_sensitive_key("my_private_key")
    assert _is_sensitive_key("privateKey")
    out = redact_payload({"my_passwd": "s3cret!"}, sink="arguments")
    assert out["my_passwd"] == "***"


def test_a16_non_string_keys_no_crash():
    """G3#91：非string key 不得崩脱敏。"""
    from hero_quant.security.redaction import _is_sensitive_key, redact_payload

    assert _is_sensitive_key(123) is False
    out = redact_payload({1: "plain", (2,): "v", "api_key": "sk-1234567890abcdef"}, sink="arguments")
    assert out[1] == "plain" and out["api_key"] == "***"


def test_a17_arguments_hex_token_redacted():
    """G3#92：ARGUMENTS 下纯hex长token必须脱敏（仅单字符重复放行）。"""
    from hero_quant.security.redaction import _redact_string

    assert _redact_string("ab12cd34ef56ab12cd34ef56ab12cd34", sink="arguments") == "***"
    assert _redact_string("x" * 40, sink="arguments") == "x" * 40  # 单字符重复非密钥


def test_a18_result_single_case_token_redacted():
    """G3#93：RESULT 下单小写长token（高熵）必须脱敏。"""
    from hero_quant.security.redaction import _redact_string

    tok = "abcdefghijklmnopqrstuvwx12345678"  # 32字符、单小写、熵高、非纯hex
    assert len(tok) >= 32
    assert _redact_string(tok, sink="result") == "***"


def test_a19_unknown_sink_matches_arguments():
    """G3#94：未知sink回退ARGUMENTS语义，不再过脱敏 benign 文本。"""
    from hero_quant.security.redaction import _redact_string

    benign = "hello world, this is a normal log message"
    assert _redact_string(benign, sink="typo_sink") == benign
    assert _redact_string("sk-1234567890abcdef", sink="typo_sink") == "***"


# ---------- tools/redaction.py ×1 ----------


def test_a20_tools_redaction_tuple_set():
    """G3#125：tools 层 tuple/set/frozenset 走脱敏，不得绕过。"""
    from hero_quant.tools.redaction import _maybe_redact, redact_tool_result

    secret = "sk-1234567890abcdef"
    assert "***" in str(_maybe_redact((secret,), sink="result"))
    assert secret not in str(_maybe_redact({secret}, sink="result"))
    assert secret not in str(_maybe_redact(frozenset((secret,)), sink="result"))
    s = redact_tool_result({"k": (secret,)}, sink="result")
    assert secret not in s and "***" in s
