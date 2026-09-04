"""Lane A1 TDD — 沙箱逃逸 25 条，每条一个 failing test（先红后绿）。

覆盖 scan_0904_g3_domain.log 中 sandbox 五文件 25 节：
__init__ 3 条、ast_guard 6 条、base 4 条、policy 3 条、runner 9 条。
fail-closed 为准，不动 lane 外文件。
"""
from __future__ import annotations

import pathlib

import pytest

REPO = pathlib.Path(__file__).parents[1]
SANDBOX = REPO / "src" / "hero_quant" / "sandbox"


def _text(name: str) -> str:
    return (SANDBOX / name).read_text(encoding="utf-8")


# ── __init__.py 3 条 ──────────────────────────────────────────────


def test_a01_init_narrow_importerror(monkeypatch):
    """宽 ImportError 降级：runner 内部 bug 必须抛，不走 stub（fail-closed）。"""
    import importlib

    import hero_quant.sandbox as sb

    sb.__dict__.pop("probe", None)  # 确保走 __getattr__ 懒加载分支
    orig_import = importlib.import_module

    def fake_import(name, *a, **k):
        if name == "hero_quant.sandbox.runner":
            raise ImportError("mock internal bug", name="hero_quant.sandbox.runner.internal_dep")
        return orig_import(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    with pytest.raises(ImportError):
        sb.__getattr__("probe")


def test_a02_stub_identity_unified():
    """stub 异常身份分叉：runner/base/包入口三方同一身份。"""
    import hero_quant.sandbox as sb
    from hero_quant.sandbox import base as b
    from hero_quant.sandbox import runner as r

    assert r.SandboxUnavailableError is b.SandboxUnavailableError
    sb.__dict__.pop("SandboxUnavailableError", None)
    assert sb.SandboxUnavailableError is b.SandboxUnavailableError


def test_a03_init_no_unreachable_branch():
    """不可达分支：_load_runner_stub 内不再为 check_source/SandboxViolation 设分支。"""
    text = _text("__init__.py")
    stub_section = text.split("def _load_runner_stub")[1].split("def __getattr__")[0]
    assert '("check_source", "SandboxViolation")' not in stub_section
    assert "('check_source', 'SandboxViolation')" not in stub_section


# ── ast_guard.py 6 条 ─────────────────────────────────────────────


def test_a04_tomli_fallback_failclosed(monkeypatch):
    """tomli 回退崩：双缺失时返回空集，不抛 ImportError（fail-closed）。"""
    import builtins

    orig = builtins.__import__

    def fake(name, *a, **k):
        if name in ("tomllib", "tomli"):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return orig(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    from hero_quant.sandbox import ast_guard as ag

    assert ag._load_pyproject_roots() == set()


def test_a05_no_cwd_poison():
    """CWD pyproject 投毒：不再回退到工作区 pyproject（fail-closed 静态根）。"""
    text = _text("ast_guard.py")
    assert 'Path.cwd() / "pyproject.toml"' not in text
    assert "cwd_candidate" not in text


def test_a06_block_file_network_attrs():
    """allowlist 库文件/网络旁路：属性级拒绝文件 I/O 与外联调用。"""
    from hero_quant.sandbox.ast_guard import check_import_allowlist as chk

    assert chk('from pathlib import Path\nx = Path("/etc/passwd").read_text()') is False
    assert chk('import httpx\nhttpx.get("http://example.com")') is False
    assert chk('import yfinance\nyfinance.download("AAPL")') is False
    # 纯导入仍放行（仅属性调用被拦）
    assert chk("import httpx") is True


def test_a07_no_silent_automerge():
    """auto-merge 破默认拒绝：需声明 pyproject 为受信输入（文档化信任边界）。"""
    text = _text("ast_guard.py")
    assert "trusted" in text.lower()


def test_a08_allowed_roots_inplace():
    """ALLOWED_ROOTS 分叉：原地 mutate，不 rebind；删除写-only 标记。"""
    from hero_quant.sandbox import ast_guard as ag

    assert not hasattr(ag, "_HAS_DYNAMIC_ATTR")
    before_id = id(ag.ALLOWED_ROOTS)
    ag.reload_allowed_roots()
    assert id(ag.ALLOWED_ROOTS) == before_id


def test_a09_no_redundant_checks():
    """冗余检查：删除不可达的 ctypes/socket/requests、subprocess、os 分支与 dunder 旗。"""
    text = _text("ast_guard.py")
    assert 'if effective in {"ctypes", "socket", "requests"}' not in text
    assert 'if effective == "subprocess"' not in text
    assert "has_dunder_arg" not in text


# ── base.py 4 条 ──────────────────────────────────────────────────


def test_a10_execute_has_timeout(monkeypatch):
    """执行无超时：两后端 subprocess.run 必须带 timeout（防 hung DoS）。"""
    import subprocess

    from hero_quant.sandbox.base import DockerBackend, LocalShellBackend

    seen: dict = {}

    def fake_run(*a, **k):
        seen.update(k)

        class R:
            stdout = ""
            stderr = ""
            returncode = 0

        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    LocalShellBackend(policy={"mode": "read-only"}).execute(["echo", "hi"])
    assert "timeout" in seen
    seen.clear()
    DockerBackend(policy={"mode": "read-only"}).execute(["echo", "hi"])
    assert "timeout" in seen


def test_a11_bwrap_tmpfs(monkeypatch, tmp_path):
    """bwrap 共享 /tmp 可写：改用 --tmpfs 隔离（防跨沙箱污染）。"""
    from hero_quant.sandbox import base as b

    monkeypatch.setattr(b, "_has_bwrap", lambda: True)
    pol = {"mode": "workspace-write", "workspaceRoot": str(tmp_path)}
    out = b.LocalShellBackend(policy=pol).confine(["echo", "hi"], pol)
    assert "--tmpfs" in out
    for i, tok in enumerate(out):
        if tok == "--bind" and i + 1 < len(out) and out[i + 1] == "/tmp":
            raise AssertionError("shared /tmp bind found")


def test_a12_enforcement_checks_bwrap(monkeypatch):
    """enforcement 虚报：无 bwrap 时不得报 full（fail-closed）。"""
    from hero_quant.sandbox import base as b

    monkeypatch.setattr(b, "_has_bwrap", lambda: False)
    assert b.LocalShellBackend(policy={"mode": "read-only"}).enforcement != "full"


def test_a13_writable_typeerror():
    """is_path_writable 捕获不足：非法类型返回 False，不抛 TypeError。"""
    from hero_quant.sandbox.base import is_path_writable as w1
    from hero_quant.sandbox.policy import is_path_writable as w2

    assert w1(None, {}) is False
    assert w2(None, {}) is False


# ── policy.py 3 条 ────────────────────────────────────────────────


def test_a14_readonly_no_tmp():
    """read-only 含 /tmp：只读模式可写根为空（名实相符）。"""
    from hero_quant.sandbox.policy import is_path_writable, resolve_policy

    pol = resolve_policy("read-only")
    assert pol.get("writableRoots") == []
    assert is_path_writable("/tmp/x", pol) is False


def test_a15_empty_path_rejected():
    """空路径未拒绝：空串永不可写（防 Path('') 归一到 cwd）。"""
    from hero_quant.sandbox.policy import is_path_writable, resolve_policy

    pol = resolve_policy("danger-full-access")
    assert is_path_writable("", pol) is False


def test_a16_canonical_fallback_failclosed(monkeypatch):
    """回退逃逸契约：cwd 解析双失败时转 ValueError，不漏 OSError。"""
    from pathlib import Path

    from hero_quant.sandbox import policy as pm

    # 让工作区缺省分支可达：canonical_path 走通，但一切 Path.resolve 均失败
    monkeypatch.setattr(pm, "canonical_path", lambda p: "/tmp")

    def _boom(self, *a, **k):
        raise OSError("gone")

    monkeypatch.setattr(Path, "resolve", _boom)
    with pytest.raises(ValueError):
        pm.resolve_policy("read-only")


# ── runner.py 9 条 ────────────────────────────────────────────────


def test_a17_probe_failclosed(monkeypatch):
    """Landlock 探针 fail-open：空/不可识别输出不得判 full。"""
    from hero_quant.sandbox import runner as r

    monkeypatch.setattr(r, "_run_probe_binary", lambda *a, **k: (0, "", ""))
    # Windows 短路分支直接返回 125，真实 fail-open 隐藏在 Linux 路径；
    # 强制走 Linux 路径验证输出归一化逻辑。
    monkeypatch.setattr(r.sys, "platform", "linux")
    ec, out, _err = r.probe_raw()
    assert ec != 0 or "fully enforced" not in out
    assert r.probe() != "full"


def test_a18_exec_sanitizes_builtins():
    """单靠 ast_guard：裸 builtins 引用（如 f=open）运行时亦拒绝（纵深防御）。"""
    from hero_quant.sandbox.runner import execute_python

    with pytest.raises(Exception):
        execute_python("f = open")


def test_a19_workspace_strict(monkeypatch, tmp_path):
    """TOCTOU+fail-open：缺失工作区不得回退未解析路径，必须抛。"""
    from hero_quant.sandbox.runner import LandlockSandbox, SandboxUnavailableError

    sb = LandlockSandbox(policy={"mode": "workspace-write"})
    monkeypatch.setattr(sb, "_verdict", lambda: "full")
    with pytest.raises(SandboxUnavailableError):
        sb.confine(["echo", "hi"], {"mode": "workspace-write", "workspaceRoot": str(tmp_path / "nope-missing")})


def test_a20_no_default_tmp(monkeypatch):
    """默认 /tmp：缺 workspace 键时 fail-closed，不授共享 /tmp。"""
    from hero_quant.sandbox.runner import LandlockSandbox, SandboxUnavailableError

    sb = LandlockSandbox(policy={"mode": "workspace-write"})
    monkeypatch.setattr(sb, "_verdict", lambda: "full")
    with pytest.raises(SandboxUnavailableError):
        sb.confine(["echo", "hi"], {"mode": "workspace-write"})


def test_a21_catches_both_errors():
    """抓错异常：同捕两处 SandboxUnavailableError（先 base 后 runner，不宽兜）。"""
    text = _text("runner.py")
    assert "BaseSandboxUnavailableError" in text or "base import SandboxUnavailableError" in text
    # 当前仅捕 runner 身份 + 宽 except Exception 兜底，视为未修
    assert "except Exception" not in text.split("verdict == \"unusable\"")[1].split("readWrite")[0]


def test_a22_launcher_failclosed(monkeypatch, tmp_path):
    """launcher 失败 fail-open：PermissionError 转 fail-closed，不静默裸跑。"""
    import subprocess

    from hero_quant.sandbox.runner import LandlockSandbox, SandboxUnavailableError

    sb = LandlockSandbox(policy={"mode": "workspace-write", "workspaceRoot": str(tmp_path)})
    monkeypatch.setattr(sb, "_verdict", lambda: "full")

    def _deny(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(subprocess, "run", _deny)
    with pytest.raises(SandboxUnavailableError):
        sb.execute(["echo", "hi"], require_enforcement=True)
    # relaxed 亦不得裸跑：必须抛或至少告警（当前实现会裸跑成功，故断言抛）
    sb2 = LandlockSandbox(policy={"mode": "workspace-write", "workspaceRoot": str(tmp_path)})
    monkeypatch.setattr(sb2, "_verdict", lambda: "full")
    monkeypatch.setattr(subprocess, "run", _deny)
    with pytest.raises(SandboxUnavailableError):
        sb2.execute(["echo", "hi"], require_enforcement=False)


def test_a23_dispatch_narrow():
    """大 except：空源不静默成功；str cmd 编程错误上浮，不吞为 dict。"""
    from hero_quant.sandbox.runner import SandboxViolation, dispatch_tool

    class Spec:
        name = "python"

    try:
        res = dispatch_tool(Spec(), {"source": ""}, {"mode": "read-only"})
    except (SandboxViolation, ValueError):
        pass  # fail-closed 抛亦可
    else:
        assert isinstance(res, dict) and "error" in res

    class Shell:
        name = "shell"

    with pytest.raises(SandboxViolation):
        dispatch_tool(Shell(), {"cmd": "echo hi"}, {"mode": "read-only"})


def test_a24_no_dead_reraise():
    """死 re-raise：删除无前缀的 except Exception: raise 包装。"""
    text = _text("runner.py")
    assert "except Exception:\n            # 保持与沙箱层一致的错误前缀\n            raise" not in text
    assert text.count("except Exception:") <= 1


def test_a25_probe_timeout_validated(monkeypatch):
    """timeout 0 变 2s：非法超时 fail-closed 返回 125，不抛 ValueError、不静默代入。"""
    import subprocess

    from hero_quant.sandbox import runner as r

    # 负超时：subprocess 抛 ValueError 时必须映射为 125（当前直接外泄）
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(ValueError("negative timeout"))
    )
    ec, _out, _err = r._run_probe_binary("whatever-launcher", timeout_ms=-1)
    assert ec == r.LAUNCHER_FAILURE_EXIT

    # timeout 0：不得静默以 2s 发起真实调用（当前悄悄代入 2）
    called: dict = {}

    def _rec(*a, **k):
        called["yes"] = True
        raise FileNotFoundError("x")

    monkeypatch.setattr(subprocess, "run", _rec)
    r._run_probe_binary("whatever-launcher", timeout_ms=0)
    assert called == {}
