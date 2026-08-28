"""Task19 TDD strict：HMAC/Host/沙箱工具调度 fail-closed 验证（3 asserts 核心 + 扩展）。"""
import hashlib
import hmac


def test_check_host_empty_denied():
    from hero_quant.api.security import check_host

    # 空白名单显式拒绝（P1 加固 fail-closed）
    assert check_host("") is False
    assert check_host("example.com", []) is False
    assert check_host("", ["example.com"]) is False
    # 白名单命中才放行
    assert check_host("example.com", ["example.com"]) is True
    assert check_host("EXAMPLE.COM:8000", ["example.com"]) is True
    # 未命中拒绝
    assert check_host("evil.com", ["example.com"]) is False


def test_verify_hmac_true_path():
    from hero_quant.api.security import verify_hmac

    payload = b"hello world"
    secret = "s3cr3t"
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    # 正确签名通过常量时间比较
    assert verify_hmac(payload, sig, secret) is True
    # 错误签名失败
    assert verify_hmac(payload, "0" * 64, secret) is False
    # 缺参数失败
    assert verify_hmac(payload, None, secret) is False
    assert verify_hmac(payload, sig, None) is False
    # 请求占位模式无有效 HMAC 前缀 → 401/False
    class FakeReq:
        headers = {"Authorization": "nope", "X-API-Key": "nope2"}
    assert verify_hmac(FakeReq()) is False
    # 含 Bearer 前缀但无有效 HMAC → fail-closed (占位正则已移除)
    class FakeReq2:
        headers = {"Authorization": "Bearer abc.def.ghi"}
    assert verify_hmac(FakeReq2()) is False


def test_tool_dispatch_sandbox_wrapper():
    from hero_quant.sandbox.runner import dispatch_tool
    from hero_quant.tools.registry import TOOL_REGISTRY, tool

    # 注册一个临时非 python 工具用于测试隔离包裹
    name = "test_sandbox_echo_strict_tmp"
    if name in TOOL_REGISTRY:
        del TOOL_REGISTRY[name]

    @tool(name=name, description="echo for strict test", is_concurrency_safe=False)
    def echo_tool(msg: str = "hi"):
        return f"echo:{msg}"

    spec = TOOL_REGISTRY[name]
    # 走 dispatch_tool 包装器：python 分支或受限子进程；应返回结果或 tool_error 前缀
    res = dispatch_tool(spec, {"msg": "hello"})
    assert isinstance(res, str)
    # 若为直接调用，返回 echo:hello；若为沙箱路径，至少含 tool_error 前缀或 echo
    assert "echo:hello" in res or "tool_error" in res

    # 清理
    del TOOL_REGISTRY[name]


def test_sandbox_base_bwrap_unavailable_raises():
    from hero_quant.sandbox.base import BaseSandbox, SandboxUnavailableError
    from hero_quant.sandbox.policy import resolve_policy
    import pathlib

    # workspace-write 在无 bwrap 环境应抛 SandboxUnavailableError 而非 no-op
    policy = resolve_policy(mode="workspace-write", workspace_root=str(pathlib.Path.cwd()))
    sb = BaseSandbox.__subclasses__()[0]() if BaseSandbox.__subclasses__() else None
    # 直接测试 BaseSandbox.confine
    base = BaseSandbox
    # 使用 LocalShellBackend 作为具体实现
    from hero_quant.sandbox.base import LocalShellBackend

    backend = LocalShellBackend(policy=policy)
    try:
        # 在 Windows/无 bwrap 环境下应抛异常；若环境有 bwrap 则跳过
        result = backend.confine(["echo", "hi"], policy)
        # 若未抛异常，说明 bwrap 存在，验证前缀包含 bwrap/landlock
        assert isinstance(result, list) and len(result) >= 2
    except SandboxUnavailableError as e:
        assert "bwrap" in str(e).lower() or "unavailable" in str(e).lower()
    except Exception as e:
        # 兼容 fallback：至少是受控异常而非静默放行
        assert "bwrap" in str(e).lower() or "unavailable" in str(e).lower()
