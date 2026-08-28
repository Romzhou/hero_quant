import pytest
import sys
import importlib


def test_fallback_stubs_raise():
    # 强制走 fallback 分支：模拟 runner 缺失时 grant_args 等应抛 SandboxUnavailableError 而非返回静默值
    orig_runner = sys.modules.get("hero_quant.sandbox.runner")
    orig_sandbox = sys.modules.get("hero_quant.sandbox")
    # 通过将 runner 设为缺失来触发 ImportError 分支
    # 使用 monkey-style 直接操作 sys.modules
    sys.modules.pop("hero_quant.sandbox.runner", None)
    # 将 hero_quant.sandbox.runner 标记为不可导入
    # 方法：临时在 sys.modules 放入 None 并删除 sandbox 以强制重新导入
    # 先移除 sandbox
    sys.modules.pop("hero_quant.sandbox", None)
    # 注入一个会抛 ModuleNotFoundError 的 finder效果：直接让 import 失败
    # 通过设置 sys.modules['hero_quant.sandbox.runner'] 为 None，Python import 会认为已加载但为 None，
    # 实际 `from .runner import` 会触发 ModuleNotFoundError/ImportError
    # 这里用更直接的方式：使用 importlib 的阻止 - 将模块放入 sys.modules 为 None
    sys.modules["hero_quant.sandbox.runner"] = None  # type: ignore
    try:
        import hero_quant.sandbox as sb  # type: ignore

        # grant_args 应抛
        with pytest.raises(Exception) as _exc:
            sb.grant_args({})
        assert "unavailable" in str(_exc.value).lower() or isinstance(_exc.value, RuntimeError)

        # probe 应抛
        with pytest.raises(Exception) as _exc:
            sb.probe()
        assert "unavailable" in str(_exc.value).lower() or isinstance(_exc.value, RuntimeError)

        # probe_raw 应抛
        with pytest.raises(Exception) as _exc:
            sb.probe_raw()
        assert "unavailable" in str(_exc.value).lower() or isinstance(_exc.value, RuntimeError)

        # validate_probe_args 应抛
        with pytest.raises(Exception) as _exc:
            sb.validate_probe_args(["landlock-run", "--probe"])
        assert "unavailable" in str(_exc.value).lower() or isinstance(_exc.value, RuntimeError)

        # LandlockSandbox execute/confine 应抛
        with pytest.raises(Exception) as _exc:
            inst = sb.LandlockSandbox()
            inst.execute(["echo", "hi"])
        assert "unavailable" in str(_exc.value).lower() or isinstance(_exc.value, RuntimeError)

        with pytest.raises(Exception) as _exc:
            inst = sb.LandlockSandbox()
            inst.confine(["echo"], {})
        assert "unavailable" in str(_exc.value).lower() or isinstance(_exc.value, RuntimeError)

        # 身份统一：SandboxViolation 来自 ast_guard，SandboxUnavailableError 来自 base
        from hero_quant.sandbox.ast_guard import SandboxViolation as AstViolation
        from hero_quant.sandbox.base import SandboxUnavailableError as BaseUE

        assert sb.SandboxViolation is AstViolation
        assert sb.SandboxUnavailableError is BaseUE

    finally:
        # 恢复模块状态
        sys.modules.pop("hero_quant.sandbox", None)
        sys.modules.pop("hero_quant.sandbox.runner", None)
        if orig_runner is not None:
            sys.modules["hero_quant.sandbox.runner"] = orig_runner
        if orig_sandbox is not None:
            sys.modules["hero_quant.sandbox"] = orig_sandbox
        else:
            # 重新加载原始 sandbox 以保证后续测试不受污染
            try:
                import hero_quant.sandbox  # noqa: F401
            except Exception:
                pass
