"""W1-C 沙箱 AST 守卫接线 — TDD 驱动验证.

覆盖：
- ast_guard.check_source 对 banned root / allowlist / 语法错误 的 fail-closed 行为
- runner LandlockSandbox / 模块级 execute_python 在 compile/exec 前调用 ast_guard.check_source
- 非 Python 载荷不受影响（普通 execute 仍可用）
- sandbox 包便捷导出
- 不改动 Landlock probe 裁决逻辑
"""
from __future__ import annotations

import pathlib

import pytest


# --- ast_guard.check_source 基础 ---

def test_ast_guard_check_source_banned_root_fail_closed():
    from hero_quant.sandbox import ast_guard
    from hero_quant.sandbox.ast_guard import SandboxViolation

    assert hasattr(ast_guard, "check_source"), "ast_guard.check_source 缺失"
    with pytest.raises(SandboxViolation):
        ast_guard.check_source("import socket")

    with pytest.raises(SandboxViolation):
        ast_guard.check_source("import os; os.system('rm -rf /')")

    with pytest.raises(SandboxViolation):
        ast_guard.check_source("import subprocess")


def test_ast_guard_check_source_allowlist_pass():
    from hero_quant.sandbox.ast_guard import check_source

    # allowlist 内片段应正常通过（无异常）
    check_source("import pandas as pd\nx = pd.DataFrame({'a':[1]})")
    check_source("import numpy\nimport joblib\nx=1")
    check_source("")  # 空源码视为通过


def test_ast_guard_check_source_syntax_error_fail_closed():
    from hero_quant.sandbox.ast_guard import SandboxViolation, check_source

    with pytest.raises(SandboxViolation) as exc:
        check_source("def foo(:\n  pass")
    msg = str(exc.value).lower()
    assert "syntax" in msg, f"expected 'syntax' in SandboxViolation message, got {exc.value!r}"

    with pytest.raises(SandboxViolation) as exc2:
        check_source("import pandas\nfor :\n  pass")
    assert "syntax" in str(exc2.value).lower()


# --- runner Python 执行分支 ---

def test_execute_python_banned_root_raises():
    from hero_quant.sandbox.runner import SandboxViolation, execute_python

    with pytest.raises(SandboxViolation):
        execute_python("import socket\nprint('should not run')")

    with pytest.raises(SandboxViolation):
        execute_python("import os\nos.system('echo hi')")

    with pytest.raises(SandboxViolation):
        execute_python("__import__('os')")


def test_execute_python_allowlist_ok():
    from hero_quant.sandbox.runner import execute_python

    result = execute_python("import pandas as pd\ndf = pd.DataFrame({'a':[1,2]})\nresult = df.shape[0]")
    assert isinstance(result, dict), f"expected dict context, got {type(result)}: {result!r}"
    assert result.get("result") == 2, f"expected result==2, got {result!r}"

    # locals_dict 分支应返回 {"globals":..., "locals":...} 且 locals 含 result
    result2 = execute_python(
        "import pandas as pd\ndf = pd.DataFrame({'a':[1,2,3]})\nresult = df.shape[0]",
        globals_dict={},
        locals_dict={},
    )
    assert isinstance(result2, dict)
    assert "globals" in result2 and "locals" in result2
    assert result2["locals"].get("result") == 3 or result2["globals"].get("result") == 3


def test_execute_python_syntax_error_fail_closed():
    from hero_quant.sandbox.runner import SandboxViolation, execute_python

    with pytest.raises(SandboxViolation):
        execute_python("def bad(:\n  pass")

    with pytest.raises(SandboxViolation):
        execute_python("import pandas\n::: syntax error :::")


def test_execute_python_calls_check_source_before_compile(monkeypatch):
    """验证 python 分支在 compile/exec 前调用 ast_guard.check_source，解析失败即拒."""
    from hero_quant.sandbox import runner
    from hero_quant.sandbox.runner import SandboxViolation

    called = {}

    orig_check = runner.ast_guard.check_source if hasattr(runner, "ast_guard") else None
    # 通过 monkeypatch 拦截 ast_guard.check_source
    import hero_quant.sandbox.ast_guard as ag

    orig = ag.check_source

    def fake_check(src):
        called["src"] = src
        # 模拟 SyntaxError 时的 fail-closed：抛 SandboxViolation
        if "SYNTAX_ERROR_MARKER" in src:
            raise SandboxViolation("syntax error mocked")
        return orig(src)

    monkeypatch.setattr(ag, "check_source", fake_check)

    # 正常 allowlist 应触发调用且通过
    runner.execute_python("import pandas\nx=1")
    assert "src" in called and "import pandas" in called["src"]

    called.clear()
    # 语法错误标记应被 check_source 拦截，且不应执行到 compile
    with pytest.raises(SandboxViolation):
        runner.execute_python("SYNTAX_ERROR_MARKER def foo(:")
    assert "SYNTAX_ERROR_MARKER" in called.get("src", "")


def test_landlock_sandbox_execute_python_wiring():
    """LandlockSandbox.execute_python 同样走 AST 守卫."""
    from hero_quant.sandbox.policy import resolve_policy
    from hero_quant.sandbox.runner import LandlockSandbox, SandboxViolation

    policy = resolve_policy(mode="read-only")
    sb = LandlockSandbox(policy=policy)

    assert hasattr(sb, "execute_python"), "LandlockSandbox 需提供 execute_python 方法"

    with pytest.raises(SandboxViolation):
        sb.execute_python("import socket")

    # allowlist 通过
    sb.execute_python("import pandas as pd\nx=1")

    with pytest.raises(SandboxViolation):
        sb.execute_python("def oops(:\n pass")


def test_non_python_payload_unaffected():
    """非 Python 载荷路径不受 AST 守卫影响."""
    from hero_quant.sandbox.policy import resolve_policy
    from hero_quant.sandbox.runner import LandlockSandbox

    # read-only 模式下普通命令应正常执行，不受 AST 影响
    ro_policy = resolve_policy(mode="read-only")
    sb = LandlockSandbox(policy=ro_policy)
    out, err, code = sb.execute(["echo", "hi"], require_enforcement=False)
    assert code == 0
    assert "hi" in out

    # 即使命令字符串包含 banned 词，也不应被 AST 拦截（仅 python 分支拦截）
    out2, err2, code2 = sb.execute(["echo", "import socket"], require_enforcement=False)
    assert code2 == 0
    assert "import socket" in out2

    # 模块级普通 execute 也不受影响（通过 LandlockSandbox 间接验证 probe 未被改）
    from hero_quant.sandbox.runner import probe

    verdict = probe()
    assert verdict in ("full", "partial", "unusable")


def test_sandbox_init_exports():
    """sandbox 包应导出便捷入口，兼容现有导入面."""
    import hero_quant.sandbox as sb_pkg

    for name in ("check_source", "SandboxViolation", "execute_python"):
        assert hasattr(sb_pkg, name), f"hero_quant.sandbox 缺少导出 {name}"

    # runner 亦应导出
    from hero_quant.sandbox import runner as r

    assert hasattr(r, "SandboxViolation")
    assert hasattr(r, "execute_python")
    assert hasattr(r, "check_source") or hasattr(r.ast_guard, "check_source")


def test_probe_logic_unchanged():
    """不改 Landlock probe 裁决逻辑：probe / probe_raw / validate_probe_args 行为保持."""
    from hero_quant.sandbox.runner import LAUNCHER_FAILURE_EXIT, probe, probe_raw, validate_probe_args

    # probe 仍返回三态之一
    v = probe()
    assert v in ("full", "partial", "unusable")

    ec, out, err = probe_raw()
    # fail-closed 前缀与退出码契约
    if ec != 0:
        assert ec == LAUNCHER_FAILURE_EXIT == 125
        assert "landlock-run:" in err

    assert validate_probe_args(["landlock-run", "--probe"]) == 0
    assert validate_probe_args(["landlock-run", "--probe", "--ro", "/tmp"]) == 125


# --- W1-C 回归：fallback 身份与委托、bare re-export 与重复消除 ---


def test_sandbox_package_violation_identity():
    from hero_quant.sandbox import ast_guard
    import hero_quant.sandbox as sb_pkg
    from hero_quant.sandbox.ast_guard import SandboxViolation as AstViolation

    assert sb_pkg.SandboxViolation is AstViolation, "包级 SandboxViolation 应与 ast_guard 同一身份，fallback 不应 fork"
    assert ast_guard.SandboxViolation is AstViolation
    # runner 亦应保持同一身份
    from hero_quant.sandbox import runner as r

    assert r.SandboxViolation is AstViolation
    assert r.check_source is ast_guard.check_source


def test_sandbox_package_check_source_is_canonical():
    from hero_quant.sandbox import ast_guard
    import hero_quant.sandbox as sb_pkg

    assert sb_pkg.check_source is ast_guard.check_source, "包级 check_source 应委托 canonical ast_guard.check_source"


def test_sandbox_init_fallback_no_fork():
    import pathlib

    text = pathlib.Path("src/hero_quant/sandbox/__init__.py").read_text(encoding="utf-8")
    assert "class SandboxViolation(RuntimeError)" not in text, "fallback forks SandboxViolation identity"
    # fallback 分支不应再定义 check_source 存根；应委托 canonical ast_guard
    fallback_section = text.split("except Exception:")[1] if "except Exception:" in text else text
    assert "def check_source(*a" not in fallback_section, "fallback check_source 应委托 canonical 而非独立存根"
    assert "class SandboxViolation" not in fallback_section, "fallback forks SandboxViolation identity"


def test_runner_no_dead_bare_reexport():
    import pathlib

    text = pathlib.Path("src/hero_quant/sandbox/runner.py").read_text(encoding="utf-8")
    assert "from .ast_guard import SandboxViolation" not in text, "runner 有死的 bare re-export，应改为 alias 委托"
    assert "from .ast_guard import check_source" not in text, "runner 有死的 bare re-export，应改为 alias 委托"


def test_runner_execute_python_dedup():
    import pathlib

    text = pathlib.Path("src/hero_quant/sandbox/runner.py").read_text(encoding="utf-8")
    assert text.count('compile(source, "<sandbox>", "exec")') == 1, "execute_python 重复未消除，应抽取公共 helper"
    assert "_execute_python_impl" in text or "_exec_python" in text, "应存在共享 helper 以消除重复"


def test_fallback_execute_python_still_fail_closed(monkeypatch):
    """fallback 的 execute_python 也应先走 AST 守卫，banned 时抛 SandboxViolation 而非 RuntimeError."""
    import importlib
    import sys

    import hero_quant.sandbox.ast_guard as ag

    # 模拟 runner 导入失败，触发 __init__.fallback 分支
    original_import = __import__

    def failing_import(name, *args, **kwargs):
        if name == "hero_quant.sandbox.runner" or name.endswith(".runner"):
            raise ImportError("simulated runner missing for TDD")
        return original_import(name, *args, **kwargs)

    # 保存并清理已加载模块
    saved = {
        mod: module
        for mod, module in sys.modules.items()
        if mod.startswith("hero_quant.sandbox")
    }
    # Keep the canonical ast_guard module loaded so the package fallback and
    # the ``ag`` reference below share the same exception class.
    for mod in ("hero_quant.sandbox", "hero_quant.sandbox.runner"):
        sys.modules.pop(mod, None)
    monkeypatch.setattr("builtins.__import__", failing_import)
    try:
        import hero_quant.sandbox as sb_pkg  # 重新触发 fallback

        # 身份保持
        assert sb_pkg.SandboxViolation is ag.SandboxViolation
        assert sb_pkg.check_source is ag.check_source
        # fail-closed：banned 应抛 SandboxViolation
        with pytest.raises(ag.SandboxViolation):
            sb_pkg.check_source("import socket")
        with pytest.raises(ag.SandboxViolation):
            sb_pkg.check_source("def bad(:\n  pass")
        # allowlist 通过
        sb_pkg.check_source("import pandas\nx=1")
        # execute_python 也应 fail-closed（先审后抛 RuntimeError/SandboxViolation，而非直接 RuntimeError）
        with pytest.raises(ag.SandboxViolation):
            sb_pkg.execute_python("import socket")
        with pytest.raises(ag.SandboxViolation):
            sb_pkg.execute_python("def bad(:\n  pass")
    finally:
        monkeypatch.undo()
        # 恢复模块
        for mod in list(sys.modules.keys()):
            if mod.startswith("hero_quant.sandbox"):
                sys.modules.pop(mod, None)
        for k, v in saved.items():
            sys.modules[k] = v
        # 重新加载正常包
        importlib.invalidate_caches()
        import hero_quant.sandbox  # noqa: F401
        import hero_quant.sandbox.runner  # noqa: F401
