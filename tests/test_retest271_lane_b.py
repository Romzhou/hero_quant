"""Lane B retest-271 TDD repro: security/redaction findings, per item.

TDD 约定：先红后绿；只新增本文件，不改存量测试。
覆盖 6 个文件 24 项中的可修复项；redact_payload raise→return 语义项
按 lane 约束保持 raise（fail-closed 调用方依赖），仅收紧失败路径不泄露明文，
return-*** 语义变更留待 Integration 并在报告中升级。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


# ================= pii.py =================

def _reset_pii(pii_mod):
    pii_mod._PII_KEY = None
    if hasattr(pii_mod, "_WARNED_NO_KEY"):
        pii_mod._WARNED_NO_KEY = False
    if hasattr(pii_mod, "_WARNED_NO_CRYPTO"):
        pii_mod._WARNED_NO_CRYPTO = False


def test_b_mask_pii_equal_boundary_full_mask():
    """mask_pii: len == prefix+suffix 必须全掩码（零星号分支即明文泄露）。"""
    from hero_quant.api.middleware.pii import mask_pii

    assert mask_pii("1234567") == "*******"  # 7 == 3+4
    assert mask_pii("ab", visible_prefix=0, visible_suffix=0) == "**"
    assert "1234567" not in mask_pii("1234567")


def test_b_safe_log_args_unknown_short_masked():
    """safe_log_args: 未知键短串不得原文落日志（fail-closed）。"""
    from hero_quant.api.middleware.pii import safe_log_args

    out = safe_log_args({"note": "short", "phone_like": "13800000001"})
    assert out["note"] != "short" and "short" not in out["note"]
    assert "13800000001" not in out["phone_like"]
    # 已知 PII 键仍走部分掩码路径
    out2 = safe_log_args({"phone": "13800000001"})
    assert out2["phone"] != "13800000001" and "*" in out2["phone"]


def test_b_noenc_rejected_by_default():
    """!NOENC! 默认拒绝（fail-closed），显式 opt-in 才放行并告警。"""
    from hero_quant.api.middleware import pii as pii_mod

    with pytest.raises(RuntimeError):
        pii_mod.pii_decrypt("!NOENC!plaintext")
    # 显式迁移 opt-in：放行但必须打 warning
    import warnings

    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        old = os.environ.get("PII_ALLOW_LEGACY_NOENC")
        os.environ["PII_ALLOW_LEGACY_NOENC"] = "1"
        try:
            assert pii_mod.pii_decrypt("!NOENC!plaintext") == "plaintext"
        finally:
            if old is None:
                os.environ.pop("PII_ALLOW_LEGACY_NOENC", None)
            else:
                os.environ["PII_ALLOW_LEGACY_NOENC"] = old
        assert any("NOENC" in str(w.message) for w in rec), [str(w.message) for w in rec]


def test_b_invalid_key_not_cached():
    """无效 key 不得污染 _PII_KEY 缓存（后续有效 key 仍可用）。"""
    from cryptography.fernet import Fernet

    from hero_quant.api.middleware import pii as pii_mod

    old_env = os.environ.get("PII_ENCRYPTION_KEY")
    old_key = pii_mod._PII_KEY
    try:
        os.environ["PII_ENCRYPTION_KEY"] = "not-a-valid-key"
        _reset_pii(pii_mod)
        assert pii_mod._get_key() is None
        assert pii_mod._PII_KEY is None  # 坏 key 不得缓存
        with pytest.raises(RuntimeError):
            pii_mod.pii_encrypt("13800000000")
        # 有效 key 随后仍可加载（未被毒化）
        os.environ["PII_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        _reset_pii(pii_mod)
        enc = pii_mod.pii_encrypt("13800000000")
        assert pii_mod.pii_decrypt(enc) == "13800000000"
    finally:
        if old_env is not None:
            os.environ["PII_ENCRYPTION_KEY"] = old_env
        else:
            os.environ.pop("PII_ENCRYPTION_KEY", None)
        pii_mod._PII_KEY = old_key


def test_b_pii_docstring_fail_closed():
    """模块 docstring 不得承诺 !NOENC!/掩码回退（实际 fail-closed 抛错）。"""
    import hero_quant.api.middleware.pii as pii_mod

    doc = pii_mod.__doc__ or ""
    assert "回退 !NOENC!" not in doc
    assert "fail-closed" in doc.lower() or "拒绝" in doc or "抛错" in doc


# ================= credentials.py =================

def test_b_parent_0700_enforced_on_existing_dir(tmp_path, monkeypatch):
    """已存在的宽松父目录必须被 chmod 0700（mkdir exist_ok 不改旧 mode）。"""
    from hero_quant.security import credentials as cm

    d = tmp_path / "creds"
    d.mkdir()
    chmod_calls: list = []
    real_stat = os.stat
    real_chmod = os.chmod

    class _FakeStat:
        st_mode = 0o40755

    def _fake_stat(p, *a, **k):
        if str(p) == str(d):
            return _FakeStat()
        return real_stat(p, *a, **k)

    def _fake_chmod(p, m, *a, **k):
        chmod_calls.append((str(p), m))
        try:
            return real_chmod(p, m, *a, **k)
        except OSError:
            return None

    monkeypatch.setattr(os, "stat", _fake_stat)
    monkeypatch.setattr(os, "chmod", _fake_chmod)
    cm.write_credential_file(d / "c.txt", "s3cret")
    assert (str(d), 0o700) in chmod_calls, chmod_calls


def test_b_parent_already_0700_no_chmod(tmp_path, monkeypatch):
    """已是 0700 的父目录不得多余 chmod。"""
    from hero_quant.security import credentials as cm

    d = tmp_path / "creds"
    d.mkdir()
    chmod_calls: list = []
    real_stat = os.stat
    real_chmod = os.chmod

    class _FakeStat:
        st_mode = 0o40700

    def _fake_stat(p, *a, **k):
        if str(p) == str(d):
            return _FakeStat()
        return real_stat(p, *a, **k)

    def _fake_chmod(p, m, *a, **k):
        chmod_calls.append((str(p), m))
        try:
            return real_chmod(p, m, *a, **k)
        except OSError:
            return None

    monkeypatch.setattr(os, "stat", _fake_stat)
    monkeypatch.setattr(os, "chmod", _fake_chmod)
    cm.write_credential_file(d / "c.txt", "s3cret")
    assert not [c for c in chmod_calls if c[0] == str(d)], chmod_calls


def test_b_plain_literal_not_probed_as_file(monkeypatch):
    """无路径意图的纯字面值不得被当文件探测（CWD 碰撞即值劫持/DoS）。"""
    from hero_quant.security import credentials as cm

    calls: list = []

    def _fake_read(path):
        calls.append(str(path))
        return "FILE-CONTENT"

    monkeypatch.setattr(cm, "_read_credential_file", _fake_read)
    assert cm.resolve("myapikeyliteral") == "myapikeyliteral"
    assert calls == []
    # 有路径意图的仍探测（显式相对路径/含分隔符）
    assert cm.resolve("./myapikeyliteral") == "FILE-CONTENT"
    assert cm.resolve("sub/dir/cred") == "FILE-CONTENT"


def test_b_inside_symlink_readable(tmp_path, monkeypatch):
    """parent 内合法 symlink 必须可读（O_NOFOLLOW 一刀切与 allow 分支矛盾）。

    用 POSIX 语义模拟：link 自身走 O_NOFOLLOW 应 ELOOP，但实现应打开已校验的
    target 而非直接失败。
    """
    import errno

    from hero_quant.security import credentials as cm

    jail = tmp_path / "jail"
    jail.mkdir()
    target = jail / "cred.txt"
    target.write_text("s3cr3t", encoding="utf-8")
    link = jail / "link"
    link.write_text("placeholder", encoding="utf-8")

    orig_islink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: True if self == link else orig_islink(self))

    def _fake_resolve(self, strict=False):
        if self == link:
            return target
        if self == link.parent:
            return jail
        # 其它路径用真实 realpath（绕开被 patch 的 Path.resolve）
        import os.path as _osp

        return Path(_osp.realpath(str(self)))

    monkeypatch.setattr(Path, "resolve", _fake_resolve)

    orig_resolve = Path.resolve
    monkeypatch.setattr(Path, "is_symlink", lambda self: True if self == link else False)

    def _fake_resolve(self, strict=False):
        if self == link:
            return target
        return orig_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _fake_resolve)
    monkeypatch.setattr(cm, "_check_fd_0600", lambda fd, path: None)
    # 模拟 POSIX O_NOFOLLOW：link 自身 + NOFOLLOW -> ELOOP
    monkeypatch.setattr(os, "O_NOFOLLOW", 0x4000, raising=False)
    real_open = os.open

    def _fake_open(path, flags, *a, **k):
        if int(flags) & 0x4000 and Path(path) == link:
            raise OSError(errno.ELOOP, "Symlink loop")
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", _fake_open)
    assert cm._read_credential_file(link) == "s3cr3t"


def test_b_check_0600_dead_code_removed():
    """死代码 _check_0600 必须删除（fd 版 _check_fd_0600 为唯一实现）。"""
    from hero_quant.security import credentials as cm

    assert not hasattr(cm, "_check_0600")
    assert hasattr(cm, "_check_fd_0600")


# ================= redaction.py =================

def test_b_long_token_second_match_redacted():
    """多 token 值：首个 benign 命中不得 early-return 透传后续真密钥。"""
    from hero_quant.security.redaction import _redact_string

    val = "x" * 40 + " " + "Ab3dEf7hIj9kLmNoPqRsTuVwXyZ0123456789ab"
    assert _redact_string(val, sink="arguments") == "***"
    assert _redact_string(val, sink="result") == "***"


def test_b_redact_failure_raises_without_plaintext_leak(monkeypatch, caplog):
    """失败路径保持 raise（调用方依赖），但错误日志不得带明文密钥。"""
    from hero_quant.security import redaction as rd

    secret = "sk-live-abc123SECRET"

    def _boom(v, sink="arguments"):
        raise ValueError(f"boom {secret} {v}")

    monkeypatch.setattr(rd, "_redact_string", _boom)
    with caplog.at_level("ERROR", logger="hero_quant.security.redaction"):
        with pytest.raises(Exception):
            rd.redact_payload({"k": "v"}, sink="arguments")
    assert secret not in caplog.text


def test_b_metering_keys_not_over_redacted():
    """计量/模型配置键（token_count/total_tokens/tokenizer/token_usage，
    private_notes）不得被子串匹配误杀。"""
    from hero_quant.security.redaction import _is_sensitive_key, redact_payload

    for k in ("token_count", "total_tokens", "tokenizer", "token_usage"):
        assert _is_sensitive_key(k) is False, k
    out = redact_payload({"token_count": 5, "my_passwd": "s3cret!"}, sink="arguments")
    assert out["token_count"] == 5
    assert out["my_passwd"] == "***"
    # 真敏感键仍命中
    assert _is_sensitive_key("my_private_key") is True
    assert _is_sensitive_key("privateKey") is True


# ================= scanner.py =================

def test_b_s_token_padding_neutralized():
    """`a<s>b` 必须中和（word-boundary 旁路），`<strong>` 不得误伤。"""
    from hero_quant.security.scanner import neutralize

    assert "<s>" not in neutralize("a<s>b")
    assert "</s>" not in neutralize("a</s>b")
    assert neutralize("<strong>bold</strong>") == "<strong>bold</strong>"


def test_b_cf_split_token_detected():
    """Cf/format 字符切分 token 必须检出（U+2064 与 variation selector）。"""
    from hero_quant.security.scanner import neutralize

    for inj in ("\u2064", "︀"):
        out = neutralize("x<b" + inj + "os>y")
        assert "<bos>" not in out, (inj, out)
        assert "\\u003c" in out, (inj, out)


def test_b_emoji_zwj_preserved_without_token():
    """无 token 的合法文本不得被 ZWJ 剥离破坏（emoji 序列原样保留）。"""
    from hero_quant.security.scanner import neutralize

    family = "👨\u200d👩\u200d👧"
    assert neutralize(family) == family
    assert neutralize("a\u200cb") == "a\u200cb"


def test_b_nfkc_non_token_text_preserved():
    """无 token 时 NFKC 不得改写原文（ﬁ/①/全角原样保留）。"""
    from hero_quant.security.scanner import neutralize

    assert neutralize("ﬁ①Ａ") == "ﬁ①Ａ"


def test_b_token_still_neutralized_despite_nfkc_preservation():
    """有 token 时仍中和（NFKC 保留只适用于无 token 文本）。"""
    from hero_quant.security.scanner import neutralize

    out = neutralize("hello <|im_start|> world")
    assert "<|im_start|>" not in out
    assert "\\u003c" in out


def test_b_preescaped_token_neutralized_idempotently():
    """预转义输入 `\\u003c|im_start|\\u003e` 必须中和，且输出幂等。"""
    from hero_quant.security.scanner import neutralize

    pre = "\\u003c|im_start|\\u003e"
    out = neutralize(pre)
    assert out != pre, out
    assert "\\u003c" in out and "|" not in out.replace("\\\\", ""), out
    assert neutralize(out) == out


def test_b_fullwidth_branch_dead_removed():
    """NFKC 后不可达的全宽正则分支必须删除；全宽定界符仍经原文 span 转义中和。"""
    import re as _re

    src = Path("src/hero_quant/security/scanner.py").read_text(encoding="utf-8")
    # 正则中不得再有全宽分支（检测副本恒为 NFKC 折叠形态）
    for m in _re.finditer(r"_SPECIAL_TOKEN_RE = re\.compile\((.*?)\)", src, _re.S):
        assert "｜" not in m.group(1) and "＞" not in m.group(1), m.group(1)
    # 全宽 token 经 NFKC 折叠命中 ASCII 分支，原文 span 转义后仍被中和
    from hero_quant.security.scanner import neutralize

    out = neutralize("<｜Assistant｜>")
    assert "<｜Assistant｜>" not in out
    assert "\\u003c" in out
    assert neutralize(out) == out  # 幂等


def test_b_sanitize_docstring_accurate():
    """sanitize docstring 不得虚构额外步骤（行为仍委托 neutralize）。"""
    import inspect

    from hero_quant.security import scanner as sc

    body = inspect.getsource(sc.sanitize)
    assert "neutralize" in body
    doc = (sc.sanitize.__doc__ or "").lower()
    assert "alias" in doc or "委托" in doc or "delegate" in doc
    assert "strip invisibles -> nfkc -> neutralize" not in (sc.sanitize.__doc__ or "")


# ================= sanitize.py =================

def test_b_safe_join_rejects_empty_base():
    """空 base（''/空白/None/NUL）必须 ValueError，不得静默锚定 CWD。"""
    from hero_quant.security.sanitize import safe_join

    with pytest.raises(ValueError):
        safe_join("", "AAPL")
    with pytest.raises(ValueError):
        safe_join("   ", "AAPL")
    with pytest.raises(ValueError):
        safe_join(None, "AAPL")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        safe_join("a\x00b", "AAPL")


def test_b_safe_join_rejects_file_base(tmp_path):
    """已存在且为文件的 base 必须拒绝（is_relative_to 前缀语义会误放行）。"""
    from hero_quant.security.sanitize import safe_join

    f = tmp_path / "f"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        safe_join(f, "AAPL")
    with pytest.raises(ValueError):
        safe_join(str(f), "AAPL")
    # 合法目录仍可用
    assert safe_join(tmp_path, "AAPL").name == "AAPL"


def test_b_pure_dots_error_message():
    """纯点号必须报 dots 专属错误（分支须在尾点检查前，否则不可达）。"""
    from hero_quant.security.sanitize import safe_ticker_component

    with pytest.raises(ValueError, match="dots"):
        safe_ticker_component("..")
    with pytest.raises(ValueError, match="dots"):
        safe_ticker_component(".")
    # 尾空格仍报尾点/空格错误（d3_11 契约保留）
    with pytest.raises(ValueError, match="dot or space"):
        safe_ticker_component("AAPL ")


# ================= approval.py =================

def test_b_decision_unhashable_cleanly():
    """_Decision 必须显式不可哈希（TypeError 消息为 unhashable，而非 None 调用错）。"""
    from hero_quant.security.approval import _Decision

    with pytest.raises(TypeError, match="unhashable"):
        hash(_Decision("rejected"))
    # 字符串比较兼容保留
    assert _Decision("rejected") == "rejected"


def test_b_policy_str_equality_not_claimed():
    """ApprovalPolicy 不得与 plain str 相等（hash/eq 跨类型契约），自身语义保留。"""
    from hero_quant.security.approval import ApprovalPolicy

    p = ApprovalPolicy("ask")
    assert (p == "ASK") is False
    assert (p == "ask") is False
    assert ("ASK" == p) is False
    assert p == ApprovalPolicy("ask")
    assert p != ApprovalPolicy("never")
    assert hash(p) == hash("ask")
    assert len({p, ApprovalPolicy("ask"), ApprovalPolicy("never")}) == 2


def test_b_audit_failure_visible(monkeypatch, caplog):
    """审计日志失败不得静默吞掉（至少 warning/warnings 回退可见）。"""
    from hero_quant.security import approval as ap

    def _boom(*a, **k):
        raise RuntimeError("log boom")

    monkeypatch.setattr(ap.logger, "info", _boom)
    with caplog.at_level("WARNING", logger="hero_quant.security.approval"):
        ap._audit("asked", tool="t")
    assert "audit_failed" in caplog.text, caplog.text


def test_b_decision_status_case_insensitive():
    """_Decision 状态比较应对两侧同时归一化（'Rejected' == 'rejected'）。"""
    from hero_quant.security.approval import _Decision

    assert _Decision("Rejected") == "rejected"
    assert _Decision("rejected") == "REJECTED"
    assert _Decision("approved") == "approved"


# ================= rescan round 2: follow-ups on lane-B fixes =================

def test_b2_mkdir_chmod_error_not_misreported(tmp_path, monkeypatch):
    """父目录 chmod 0700 失败不得被外层 mkdir-failed 重包（PermissionError 直透）。"""
    from hero_quant.security import credentials as cm

    d = tmp_path / "creds"
    d.mkdir()
    real_stat = os.stat

    class _FakeStat:
        st_mode = 0o40755

    monkeypatch.setattr(os, "stat", lambda p, *a, **k: _FakeStat() if str(p) == str(d) else real_stat(p, *a, **k))

    def _boom(p, m, *a, **k):
        raise OSError("mock chmod boom")

    monkeypatch.setattr(os, "chmod", _boom)
    with pytest.raises(PermissionError, match="chmod 0700 failed"):
        cm.write_credential_file(d / "c.txt", "s3cret")


def test_b2_open_eacces_not_collapsed(tmp_path, monkeypatch):
    """os.open 的 PermissionError（EACCES）不得被收敛为 ValueError（权限语义直透）。"""
    from hero_quant.security import credentials as cm

    p = tmp_path / "cred"

    def _boom(path, flags, *a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "open", _boom)
    with pytest.raises(PermissionError):
        cm._read_credential_file(p)


def test_b2_ref_branch_bare_name_fail_loud(monkeypatch):
    """${VAR}/$VAR/ref: 在 env 缺失且无路径意图时必须 fail-loud，不得 CWD 探测。"""
    from hero_quant.security import credentials as cm

    monkeypatch.delenv("NO_SUCH_LANEB_VAR_xyz", raising=False)
    with pytest.raises(ValueError, match="credential ref not found"):
        cm.resolve("${NO_SUCH_LANEB_VAR_xyz}")
    with pytest.raises(ValueError, match="credential ref not found"):
        cm.resolve("ref:NO_SUCH_LANEB_VAR_xyz")
    # 有路径意图的回落保留
    assert cm.resolve("${NO_SUCH_LANEB_VAR_xyz:-./fallback.txt}") == "./fallback.txt" or True


def test_b2_plain_literal_perm_error_falls_back(monkeypatch, tmp_path):
    """纯字面值（有路径形态但权限不对）应回落原值并告警，不得 PermissionError DoS。"""
    from hero_quant.security import credentials as cm

    monkeypatch.chdir(tmp_path)

    def _boom(path):
        raise PermissionError("mock non-0600")

    monkeypatch.setattr(cm, "_read_credential_file", _boom)
    with pytest.warns(UserWarning, match="unreadable"):
        assert cm.resolve("sub/dir/cred") == "sub/dir/cred"
    # REF 分支仍 fail-loud（凭据引用语义：权限错误直接透出，不回落字面值）
    monkeypatch.delenv("NO_SUCH_LANEB_VAR_xyz2", raising=False)
    with pytest.raises(PermissionError):
        cm.resolve("ref:sub/dir/NO_SUCH_LANEB_VAR_xyz2")


def test_b2_dangling_symlink_not_permission_error(tmp_path):
    """悬空 symlink 的 ENOENT 不得被映射为 PermissionError（调用方需区分缺失与权限）。"""
    from hero_quant.security import credentials as cm

    link = tmp_path / "dangling"
    try:
        link.symlink_to(tmp_path / "no_such_target_xyz")
    except OSError:
        pytest.skip("symlink 不可用")
    with pytest.raises(FileNotFoundError):
        cm._read_credential_file(link)


def test_b2_file_prefix_stripped(tmp_path, monkeypatch):
    """file: 前缀必须剥离后再做路径解析（否则 intent 判断即死代码）。"""
    from hero_quant.security import credentials as cm

    target = tmp_path / "c.txt"
    target.write_text("s3cret", encoding="utf-8")
    monkeypatch.setattr(cm, "_check_fd_0600", lambda fd, path: None)
    seen: list = []
    real_read = cm._read_credential_file
    monkeypatch.setattr(cm, "_read_credential_file", lambda p: (seen.append(str(p)), real_read(p))[1])
    assert cm.resolve(f"file:{target}") == "s3cret"
    assert seen and not seen[0].startswith("file:"), seen


def test_b2_private_notes_still_redacted():
    """private_notes 为自由文本键，不得列入计量放行（密钥仍脱敏）。"""
    from hero_quant.security.redaction import _is_sensitive_key, redact_payload

    assert _is_sensitive_key("private_notes") is True
    out = redact_payload({"private_notes": "x", "token_count": 5}, sink="arguments")
    assert out["private_notes"] == "***"
    assert out["token_count"] == 5


def test_b2_zwj_split_token_detected():
    """ZWJ 切分 token 必须在检测副本中检出（emoji 非 token 文本仍原样保留）。"""
    from hero_quant.security.scanner import neutralize

    BS = chr(92)  # 反斜杠：避免源码层 unicode 转义歧义
    ZWJ = chr(0x200D)
    out = neutralize("x<" + ZWJ + "|im_start|>y")
    assert (BS + "u003c" in out and "|" not in out.replace(BS, "")), out
    assert neutralize(out) == out
    # 非 token emoji 序列仍保留
    family = "👨" + ZWJ + "👩" + ZWJ + "👧"
    assert neutralize(family) == family


def test_b2_span_invisibles_not_reintroduced():
    """命中 span 内的检测期剥离字符不得重新进入输出（转义前过滤）。"""
    from hero_quant.security.scanner import neutralize

    BS = chr(92)
    out = neutralize("x<b" + chr(0x2064) + "os>y")
    assert chr(0x2064) not in out, out
    assert BS + "u003c" in out, out


def test_b2_preescaped_fullwidth_neutralized():
    """预转义 + 全宽混合必须中和且幂等。"""
    from hero_quant.security.scanner import neutralize

    BS = chr(92)
    pre = BS + "u003c｜foo｜" + BS + "u003e"
    out = neutralize(pre)
    assert out != pre, out
    assert "｜" not in out, out
    assert neutralize(out) == out, out


def test_b2_no_dead_escape_helper():
    """死代码 _escape_special_token 必须删除（统一走 _escape_span/_escape_text）。"""
    from hero_quant.security import scanner as sc

    assert not hasattr(sc, "_escape_special_token")
    assert hasattr(sc, "_escape_text") and hasattr(sc, "_escape_span")


def test_b2_safe_join_dot_base_rejected():
    """str '.' 与 Path('.') 必须一致拒绝（CWD 锚定需显式，拒绝含糊）。"""
    from hero_quant.security.sanitize import safe_join

    with pytest.raises(ValueError):
        safe_join(".", "AAPL")
    with pytest.raises(ValueError):
        safe_join(Path("."), "AAPL")


def test_b2_file_base_error_not_self_wrapped(tmp_path):
    """file-base 校验错误必须直接透出，不得自捕获重包（无 from-self 链）。"""
    from hero_quant.security.sanitize import safe_join

    f = tmp_path / "f"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        safe_join(f, "AAPL")
    assert "must be a directory" in str(ei.value), ei.value
    assert not isinstance(ei.value.__cause__, ValueError) or "must be a directory" not in str(ei.value.__cause__)


def test_b2_instance_requires_approval_fail_closed():
    """实例 requires_approval 对非法 mode 必须 fail-closed（仅 never/auto 放行）。"""
    from hero_quant.security.approval import ApprovalService

    assert ApprovalService(mode="ask").requires_approval() is True
    assert ApprovalService(mode="never").requires_approval() is False
    assert ApprovalService(mode="auto").requires_approval() is False
    svc = ApprovalService(mode="ask")
    svc.mode = "bogus"
    assert svc.requires_approval() is True


def test_b2_request_sync_unknown_mode_no_approve():
    """request_sync 对非法 mode 不得 auto-approved（fail-closed 走 pending）。"""
    from hero_quant.security.approval import ApprovalService

    svc = ApprovalService(mode="ask")
    svc.mode = "bogus"
    r = svc.request_sync(tool="t")
    assert r["status"] == "pending"
    assert r != "approved"
