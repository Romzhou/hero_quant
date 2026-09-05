"""沙箱执行器 — Landlock 隔离的 fail-closed 执行层。

职责：把 ``BaseSandbox.confine`` 的授权结果落地为 ``landlock-run`` 子进程调用，
对外暴露 ``probe()`` 作为唯一可用性信号，不直接检查二进制是否存在。

安全设计：fail-closed 原则——当 ``workspace-write`` 且 ``require_enforcement=True``
但内核不支持或二进制缺失时抛 ``SandboxUnavailableError`` 且绝不执行原命令；
仅 Linux 尝试真实探针，Darwin/Windows 直接判定 ``unusable``；探针与执行均带
超时，错误统一前缀 ``landlock-run: `` 与退出码 125，便于调用方统一识别。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import ast_guard
from .base import BaseSandbox
from .base import SandboxUnavailableError

# 便捷导出：保持与 ast_guard 同一身份，避免 bare re-export 被 ruff 误判为死代码
# 异常身份统一：以 base.SandboxUnavailableError 为 canonical，不再自立类（防分叉）
SandboxViolation = ast_guard.SandboxViolation
check_source = ast_guard.check_source

# ---------------------------------------------------------------------------
# 合约常量（与 docs/cli-contract.md 及 landlock-run 二进制保持一致，勿随意改动）
# ---------------------------------------------------------------------------
LAUNCHER_FAILURE_EXIT: int = 125
LAUNCHER_BIN: str = "landlock-run"

# 平台无关的执行结果类型
LandlockEnforcement = str  # 'full' | 'partial' | 'unusable'

# 合约要求的错误前缀——调用方同时依赖退出码 125 与此外缀来判定启动器失败
_FATAL_PREFIX = "landlock-run: "
_NOT_ENFORCED_MSG = "landlock is not enforced by this kernel (ABI unsupported or disabled)"


# ---------------------------------------------------------------------------
# 启动器路径解析 —— 仅做路径推导，不检查存在性；是否可用以 probe() 为准
# 刻意不做存在性检查，避免 TOCTOU；探针是唯一可信信号
# ---------------------------------------------------------------------------

def launcher_path(
    resolve_via_which: bool = True,
    fallback: str | None = None,
) -> str:
    """推导本机 ``landlock-run`` 绝对路径（不检查存在性，可用性由 probe 判定）。"""
    # 允许通过环境变量显式覆盖，便于测试与运维注入
    env = os.environ.get("HERO_LANDLOCK_BIN", "").strip()
    if env:
        return env
    if resolve_via_which:
        found = shutil.which(LAUNCHER_BIN)
        if found:
            return found
    if fallback:
        return fallback
    # 回退路径刻意指向包内不存在的绝对路径，使 probe 能稳定判定为 unusable
    # 避免使用 cwd 相对路径，防止路径劫持
    try:
        # src/hero_quant/sandbox/runner.py -> src/hero_quant -> hero_quant -> repo root guess
        repo_root = Path(__file__).resolve().parents[3]
        candidate = repo_root / "node_modules" / f"@deepseek-ai/node-addon-landlock-run-{sys.platform}-{os.uname().machine if hasattr(os, 'uname') else 'x64'}" / "bin" / LAUNCHER_BIN
        return str(candidate)
    except (IndexError, OSError):
        pass
    # 最后回退为裸二进制名，依赖 PATH 解析；缺失时探针将返回 unusable
    return LAUNCHER_BIN


# ---------------------------------------------------------------------------
# CLI 合约校验 —— 镜像 landlock-run 的手写 argv 解析，语法错误直接返回 125
# 绝不执行命令，仅做语法合法性检查
# ---------------------------------------------------------------------------

def validate_probe_args(argv: List[str]) -> int:
    """按 landlock-run CLI 文法校验 argv，合法返回 0 否则返回 125。"""
    if not argv:
        return LAUNCHER_FAILURE_EXIT
    # argv[0] is binary name, rest is args
    args = argv[1:]
    if not args:
        return LAUNCHER_FAILURE_EXIT

    # --probe 与授权/命令互斥，出现时必须独占
    if "--probe" in args:
        if len(args) != 1 or args[0] != "--probe":
            return LAUNCHER_FAILURE_EXIT
        return 0

    # 解析授权与分隔符
    i = 0
    has_seen_sep = False
    command_start = -1
    while i < len(args):
        token = args[i]
        if token in ("--ro", "--rw"):
            # 授权标志后必须跟路径参数
            if i + 1 >= len(args):
                return LAUNCHER_FAILURE_EXIT
            nxt = args[i + 1]
            if not nxt or nxt.startswith("-"):
                return LAUNCHER_FAILURE_EXIT
            i += 2
        elif token == "--":
            has_seen_sep = True
            command_start = i + 1
            break
        else:
            # 未知标志一律视为用法错误，防止注入额外参数
            return LAUNCHER_FAILURE_EXIT

    if not has_seen_sep:
        return LAUNCHER_FAILURE_EXIT
    if command_start < 0 or command_start >= len(args):
        return LAUNCHER_FAILURE_EXIT
    # 命令不能为空
    if not args[command_start]:
        return LAUNCHER_FAILURE_EXIT
    return 0


# ---------------------------------------------------------------------------
# 授权参数构造 —— 将 {readOnly, readWrite} 映射为 --ro/--rw 序列
# ---------------------------------------------------------------------------

def grant_args(grants: Dict[str, List[str]]) -> List[str]:
    """由授权字典构造 --ro/--rw 参数列表，只读在前、读写在后，保持调用方顺序。"""
    out: List[str] = []
    for ro in grants.get("readOnly", []) or []:
        out.extend(["--ro", str(ro)])
    for rw in grants.get("readWrite", []) or []:
        out.extend(["--rw", str(rw)])
    return out


# ---------------------------------------------------------------------------
# 功能探针 —— 实际执行 landlock-run --probe 并归类结果
# ---------------------------------------------------------------------------

def _run_probe_binary(launcher: str, timeout_ms: int = 2000) -> Tuple[int, str, str]:
    """执行 ``launcher --probe`` 并返回 (exit_code, stdout, stderr)，带超时保护防挂死。"""
    # 非法超时 fail-closed：0/负值不再静默代入 2s，直接合成失败输出
    if not isinstance(timeout_ms, (int, float)) or timeout_ms <= 0:
        return LAUNCHER_FAILURE_EXIT, "", f"{_FATAL_PREFIX}invalid probe timeout: {timeout_ms!r}\n"
    try:
        result = subprocess.run(
            [launcher, "--probe"],
            timeout=timeout_ms / 1000,
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout or "", result.stderr or ""
    except FileNotFoundError:
        return LAUNCHER_FAILURE_EXIT, "", f"{_FATAL_PREFIX}cannot execute {launcher}: No such file or directory\n"
    except OSError as e:
        return LAUNCHER_FAILURE_EXIT, "", f"{_FATAL_PREFIX}{e}\n"
    except subprocess.TimeoutExpired:
        return LAUNCHER_FAILURE_EXIT, "", f"{_FATAL_PREFIX}probe timed out after {timeout_ms}ms\n"
    except ValueError as e:
        # 超时/参数非法 fail-closed 映射为 125，不外泄 ValueError 破坏探针三态契约
        return LAUNCHER_FAILURE_EXIT, "", f"{_FATAL_PREFIX}probe invalid argument: {e}\n"


def probe_raw(
    launcher: str | None = None,
    timeout_ms: int = 2000,
) -> Tuple[int, str, str]:
    """原始探针：返回 (exit_code, stdout, stderr)；非 Linux 或缺失二进制时合成合约要求的失败输出。"""
    # 校验探针语法（合约要求 --probe 不带其他参数）
    bin_path = launcher or launcher_path()
    # 非 Linux 平台本质上不支持 Landlock，直接合成 unusable 结果
    if sys.platform != "linux":
        return LAUNCHER_FAILURE_EXIT, "", f"{_FATAL_PREFIX}{_NOT_ENFORCED_MSG}\n"

    # Linux 下尝试真实二进制
    exit_code, out, err = _run_probe_binary(bin_path, timeout_ms=timeout_ms)
    # 归一化：失败信息必须携带 fatal 前缀以满足合约
    if exit_code != 0 and not err.startswith(_FATAL_PREFIX):
        err = f"{_FATAL_PREFIX}{err}" if err else f"{_FATAL_PREFIX}{_NOT_ENFORCED_MSG}\n"
    if exit_code == 0:
        # 安全：仅显式 enforced 标识判 full；空/不可识别输出 fail-closed 转失败，
        # 防伪造/损坏 launcher 以空输出冒充完全隔离
        if "partially enforced" not in out and "fully enforced" not in out:
            return LAUNCHER_FAILURE_EXIT, out, f"{_FATAL_PREFIX}{_NOT_ENFORCED_MSG}\n"
    return exit_code, out, err


def probe(
    launcher: str | None = None,
    timeout_ms: int = 2000,
) -> LandlockEnforcement:
    """功能探针裁决：返回 'full' | 'partial' | 'unusable'，基于实际执行而非版本检查。"""
    exit_code, out, _err = probe_raw(launcher=launcher, timeout_ms=timeout_ms)
    if exit_code != 0:
        return "unusable"
    if "partially enforced" in out:
        return "partial"
    return "full"


def _execute_python_impl(
    source: str,
    globals_dict: dict | None = None,
    locals_dict: dict | None = None,
) -> dict:
    """共享的 Python 执行实现：先 AST 守卫再 compile/exec，消除重复。

    纵深防御（ast_guard 为第一道，此处为第二道）：
    - 空源 fail-closed 拒绝，不静默返回成功空 dict；
    - 裸危险名引用（如 ``f = open``）亦拒绝，不止拦 Call 形态；
    - 注入受限 builtins（最小安全子集），同进程 exec 不暴露完整内置能力；
    - 文件/网络隔离仍由 OS 层（Landlock/bwrap/Docker）强制，本函数不声称 containment。
    """
    import ast as _ast
    import builtins as _builtins

    if not source or not source.strip():
        raise SandboxViolation("empty source refused (fail-closed)")
    ast_guard.check_source(source)
    # 裸名引用检查：Load 上下文引用 eval/exec/open 等即拒（防 ``f = open`` 绕过 Call 拦截）
    _tree = _ast.parse(source)
    for _node in _ast.walk(_tree):
        if isinstance(_node, _ast.Name) and isinstance(_node.ctx, _ast.Load):
            if _node.id in ast_guard.BANNED_CALL_NAMES or _node.id in ast_guard.BANNED_GETATTR_NAMES:
                raise SandboxViolation(f"banned builtin reference: {_node.id}")
    code = compile(source, "<sandbox>", "exec")
    _safe_names = (
        "isinstance", "issubclass", "len", "range", "enumerate",
        "str", "int", "float", "bool", "list", "dict", "tuple", "set", "frozenset",
        "min", "max", "sum", "abs", "round", "sorted", "zip", "map", "filter",
        "any", "all", "repr", "Exception", "ValueError", "TypeError", "RuntimeError",
    )
    _safe_builtins = {k: getattr(_builtins, k) for k in _safe_names if hasattr(_builtins, k)}
    _real_import = _builtins.__import__
    g_dict: dict = {} if globals_dict is None else dict(globals_dict)
    # 调用方显式给的 __builtins__/预注入危险模块不得信任：强制覆盖，防预注入绕过
    # （如 globals={'os': os} 或 __builtins__=完整内置可直接拿到 system/eval）。
    for _bad in (ast_guard.BANNED_CALL_NAMES | ast_guard.BANNED_GETATTR_NAMES | {"__builtins__"} | set(ast_guard.BANNED_IMPORT_ROOTS)):
        g_dict.pop(_bad, None)
    if isinstance(locals_dict, dict):
        for _bad in (ast_guard.BANNED_CALL_NAMES | ast_guard.BANNED_GETATTR_NAMES | {"__builtins__"} | set(ast_guard.BANNED_IMPORT_ROOTS)):
            if _bad in locals_dict:
                raise SandboxViolation(f"banned name in locals_dict: {_bad}")

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # type: ignore[no-redef]
        """运行时导入门控：用户源码的直接导入走 allowlist，第三方库传递导入放行。

        背景：静态 check_source 已拒绝用户源码中的危险导入/``__import__`` 调用，
        但运行时仍需纵深防御——调用方经 globals_dict 预注入或动态构造
        （如 ``python -c "__import__('os').system(...)"`` 经 execute_python 路径）
        仍可拿到真实 ``__import__``。故此处不透传裸实现。
        区分依据：import 语句/显式 __import__ 的 globals 即用户 g_dict；
        第三方库内部传递导入的 globals 为其自身模块命名空间，直接放行以免破坏
        pandas/numpy 等内部 ``import os`` 传递导入。
        """
        try:
            _is_user = globals is None or globals is g_dict
        except Exception:
            _is_user = True
        if _is_user:
            if not isinstance(name, str) or not name:
                raise SandboxViolation("banned import: empty module name")
            if level != 0:
                raise SandboxViolation("banned relative import (fail-closed)")
            _root = name.split(".")[0]
            if _root in ast_guard.BANNED_IMPORT_ROOTS or name in ast_guard.BANNED_IMPORT_ROOTS:
                raise SandboxViolation(f"banned import: {name}")
            _allowed = ast_guard.get_allowed_roots()
            if _root not in _allowed:
                raise SandboxViolation(f"import allowlist violation: {name}")
        return _real_import(name, globals, locals, fromlist, level)

    _safe_builtins["__import__"] = _guarded_import
    # 纵深防御：调用方显式给的 __builtins__ 不予尊重，一律覆盖为受限子集
    g_dict["__builtins__"] = _safe_builtins
    if locals_dict is None:
        exec(code, g_dict)  # type: ignore[arg-type]
        return g_dict
    l_dict: dict = dict(locals_dict)
    exec(code, g_dict, l_dict)  # type: ignore[arg-type]
    return {"globals": g_dict, "locals": l_dict}


# ---------------------------------------------------------------------------
# LandlockSandbox — 在 BaseSandbox 之上的 fail-closed 封装
# ---------------------------------------------------------------------------

class LandlockSandbox(BaseSandbox):
    """Landlock 感知的沙箱，基于探针结果决定是否以 landlock-run 包裹执行。

    安全不变量：workspace-write 模式下若探针为 unusable 且 require_enforcement
    为真，execute() 必须抛 SandboxUnavailableError 且不执行命令（fail-closed）；
    其他模式不做 Landlock 包裹，由上层隔离保证。
    """

    def __init__(
        self,
        policy: Dict | None = None,
        launcher: str | None = None,
    ):
        self._policy: Dict = dict(policy) if isinstance(policy, dict) else {}
        self._launcher: str = launcher or launcher_path()
        self._cached_verdict: str | None = None
        self._verdict_lock = threading.Lock()

    def _verdict(self) -> str:
        if self._cached_verdict is None:
            with self._verdict_lock:
                if self._cached_verdict is None:
                    self._cached_verdict = probe(launcher=self._launcher)
        return self._cached_verdict

    @property
    def enforcement(self) -> str:
        """返回当前主机与策略下的隔离等级。"""
        v = self._verdict()
        # read-only 模式无需 Landlock，视为 full
        mode = self._policy.get("mode") if isinstance(self._policy, dict) else None
        if mode == "read-only":
            return "full"
        # workspace-write 直接映射探针结果
        if v in ("full", "partial"):
            return v
        # unusable 保持原样，调用方可降级为 partial 处理
        return "unusable"

    def confine(self, argv: List[str], policy: Dict) -> List[str]:  # type: ignore[override]
        """构造隔离前缀：可用时返回 landlock-run 包裹，否则回退到基类 bwrap 逻辑。"""
        # 合并存储策略与单次调用策略（单次优先）
        merged: Dict = {}
        if isinstance(self._policy, dict):
            merged.update(self._policy)
        if isinstance(policy, dict):
            merged.update(policy)
        mode = merged.get("mode")
        # 非 workspace-write 不在 Python 层做 Landlock 包裹
        if mode != "workspace-write":
            return super().confine(argv, merged)

        verdict = self._verdict()
        if verdict == "unusable":
            # 无法强隔离时尝试 bwrap 回退；bwrap 不可用时由基类抛 SandboxUnavailableError，
            # 此处捕获后返回 no-op，交由 execute 的 fail-closed 分支按 require_enforcement 决定是否执行
            try:
                return super().confine(argv, merged)
            except SandboxUnavailableError:
                # base 与 runner 已统一身份，此处单捕即覆盖两处来源，不再宽 except 兜底
                return list(argv)

        # 构造授权：根目录只读，工作区与 /tmp 可写
        # 缺 workspace 键 fail-closed：不再默认共享 /tmp（防跨租户干扰）
        ws = merged.get("workspaceRoot") or merged.get("workspace_root") or merged.get("canonicalPath")
        if not ws:
            raise SandboxUnavailableError(
                f"{_FATAL_PREFIX}workspaceRoot required for workspace-write (exit {LAUNCHER_FAILURE_EXIT})"
            )
        # symlink 拒绝 + fd 持有校验（TOCTOU 收敛）：工作区本身若为符号链接直接
        # fail-closed；Linux 上以 O_NOFOLLOW|O_DIRECTORY 打开目录 fd 并经
        # /proc/self/fd 复核 canonical，防止校验→使用窗口内的 symlink/dir 置换。
        # 非 Linux（无 O_NOFOLLOW/O_DIRECTORY）沿用 strict 解析 + 目录校验。
        _use_fd = hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY") and sys.platform == "linux"
        _ws_fd: int | None = None
        try:
            if Path(ws).is_symlink():
                raise SandboxUnavailableError(
                    f"{_FATAL_PREFIX}workspaceRoot symlink rejected: {ws} (exit {LAUNCHER_FAILURE_EXIT})"
                )
        except OSError as e:
            raise SandboxUnavailableError(
                f"{_FATAL_PREFIX}workspaceRoot unavailable: {ws} (exit {LAUNCHER_FAILURE_EXIT})"
            ) from e
        try:
            if _use_fd:
                try:
                    _ws_fd = os.open(ws, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
                except OSError as e:
                    raise SandboxUnavailableError(
                        f"{_FATAL_PREFIX}workspaceRoot unavailable: {ws} (exit {LAUNCHER_FAILURE_EXIT})"
                    ) from e
                # 经 fd 复核：校验与授权同一打开实例，置换窗口收敛到 open 原子点
                ws_canonical = str(Path(f"/proc/self/fd/{_ws_fd}").resolve(strict=True))
            else:
                # strict 解析 + 目录校验：缺失/悬空链接 fail-closed，不回退未解析路径
                ws_canonical = str(Path(ws).resolve(strict=True))
        except (OSError, RuntimeError, ValueError) as e:
            raise SandboxUnavailableError(
                f"{_FATAL_PREFIX}workspaceRoot unavailable: {ws} (exit {LAUNCHER_FAILURE_EXIT})"
            ) from e
        finally:
            if _ws_fd is not None:
                try:
                    os.close(_ws_fd)
                except OSError:
                    pass
        # 注：fd 在授权构造前已关闭，残余 TOCTOU（授权→subprocess.run 间隔）仍存在；
        # 完全消除需 Landlock/bwrap 内核级挂载隔离，此处仅收敛校验窗口并文档化。
        if not Path(ws_canonical).is_dir():
            raise SandboxUnavailableError(
                f"{_FATAL_PREFIX}workspaceRoot not a directory: {ws} (exit {LAUNCHER_FAILURE_EXIT})"
            )
        grants = {
            "readOnly": ["/"],
            # 默认不再共享 /tmp（防跨租户干扰）：仅当策略显式要求
            # （grantSharedTmp=True，须为调用方显式声明的 per-job 私有 tmp）才授予。
            "readWrite": [ws_canonical] + (["/tmp"] if merged.get("grantSharedTmp") is True else []),
        }
        prefix = [self._launcher] + grant_args(grants) + ["--"]
        return prefix + [str(x) for x in argv]

    def execute(  # type: ignore[override]
        self,
        cmd: List[str],
        require_enforcement: bool = True,
        timeout: float | None = None,
    ) -> Tuple[str, str, int]:
        """以 fail-closed 语义执行命令；强隔离缺失时抛异常而非执行。"""
        if isinstance(cmd, str):  # type: ignore[unreachable]
            raise SandboxViolation("str cmd not allowed; use List[str]")
        if not isinstance(cmd, (list, tuple)):
            raise SandboxViolation("cmd must be List[str]; str not allowed")

        # list argv 路径
        argv = [str(x) for x in cmd]
        mode = self._policy.get("mode") if isinstance(self._policy, dict) else None
        if require_enforcement and mode == "workspace-write" and self._verdict() == "unusable":
            raise SandboxUnavailableError(
                f"{_FATAL_PREFIX}{_NOT_ENFORCED_MSG} (exit {LAUNCHER_FAILURE_EXIT}); "
                f"workspace-write requires Landlock but probe is unusable; command not run"
            )
        # 构造隔离后的 argv（可能是 landlock 前缀或 bwrap/no-op 回退）
        wrapped = self.confine(argv, {})
        # 非 Linux 下 landlock 前缀无意义，避免 ENOENT；已在上方处理 require_enforcement 分支
        # fail-closed：启动器缺失/失败一律抛，不再回退裸 argv（loud 警告亦不执行——
        # 警告后执行即 fail-open，与模块 fail-closed 契约矛盾）。
        if wrapped and wrapped[0] == self._launcher and sys.platform != "linux":
            raise SandboxUnavailableError(
                f"{_FATAL_PREFIX}launcher not usable on {sys.platform}: {self._launcher} "
                f"(exit {LAUNCHER_FAILURE_EXIT}); command not run"
            )
        # Linux 下若二进制缺失，探针已为 unusable，此处包裹应为 no-op；防御性再检查
        if wrapped and wrapped[0] == self._launcher:
            if not Path(self._launcher).exists() and shutil.which(self._launcher) is None:
                raise SandboxUnavailableError(f"{_FATAL_PREFIX}launcher not found: {self._launcher} (exit {LAUNCHER_FAILURE_EXIT})")

        try:
            result = subprocess.run(wrapped, shell=False, capture_output=True, text=True, timeout=timeout)
        except (FileNotFoundError, PermissionError, OSError) as e:
            # 启动器失败 fail-closed 收口：缺失/无权限/不可执行一律转 SandboxUnavailableError，
            # 宽松模式亦不再回退裸 argv（fail-open 已删）。
            raise SandboxUnavailableError(f"{_FATAL_PREFIX}{e} (exit {LAUNCHER_FAILURE_EXIT})") from e
        except subprocess.TimeoutExpired as e:
            raise SandboxUnavailableError(
                f"{_FATAL_PREFIX}execution timed out: {e} (exit {LAUNCHER_FAILURE_EXIT})"
            ) from e
        return result.stdout, result.stderr, result.returncode

    def execute_python(  # type: ignore[no-redef]
        self,
        source: str,
        globals_dict: dict | None = None,
        locals_dict: dict | None = None,
    ) -> dict:
        """Python 执行分支：compile/exec 前先经 ast_guard.check_source 审查，fail-closed."""
        return _execute_python_impl(source, globals_dict, locals_dict)


def execute_python(
    source: str,
    globals_dict: dict | None = None,
    locals_dict: dict | None = None,
) -> dict:
    """模块级 Python 执行入口：先 AST 审查再 compile/exec，fail-closed."""
    return _execute_python_impl(source, globals_dict, locals_dict)


def dispatch_tool(tool_spec: Any, args: dict | None = None, policy: dict | None = None) -> Any:
    """受限子进程的工具调度包装器（Wave6 安全加固）。

    - python 分支仍走 AST 守卫的 execute_python
    - 非 python 工具走 LandlockSandbox 隔离的子进程；若沙箱不可用则捕获 SandboxUnavailableError
      并返回 ``tool_error: ...`` 前缀结果，由调用方统一识别为失败而非静默放行
    """
    if args is None:
        args = {}
    pol = policy or {}
    name = getattr(tool_spec, "name", "") if tool_spec is not None else ""
    # python 工具走受控 exec（fail-closed 上浮 SandboxViolation/ValueError，不吞）
    if name in ("execute_python", "python", "run_python"):
        src = args.get("source") or args.get("code") or ""
        return execute_python(src, args.get("globals"), args.get("locals"))
    # 非 python：尝试通过沙箱隔离执行；工具本身可能是任意 callable，回退为直接调用
    # 异常身份已统一（base 为 canonical），单捕即覆盖两处来源；编程错误
    # （SandboxViolation/TypeError）与子进程超时上浮，不吞为 dict（fail-closed）
    try:
        # 若 tool_spec 有可调用 func，则尝试隔离包裹其命令形式
        sandbox = LandlockSandbox(policy=pol)
        require_enforcement = pol.get("require_enforcement", pol.get("mode") == "workspace-write")
        # 若 args 含 argv/cmd 则走子进程，否则直接调用 func
        cmd = args.get("cmd") or args.get("argv") or args.get("command")
        if isinstance(cmd, str):
            raise SandboxViolation("str cmd not allowed; use List[str]")
        if isinstance(cmd, (list, tuple)):
            out, err, code = sandbox.execute(list(cmd), require_enforcement=require_enforcement)  # type: ignore[arg-type]
            if code != 0:
                return {"error": f"tool_error: {err or out} (code {code})"}
            return out
        # 回退直接调用 — 默认禁止，直调仅在显式 allow_direct_call 且非 workspace-write 时允许
        func = getattr(tool_spec, "func", None)
        if callable(func):
            mode = pol.get("mode") if isinstance(pol, dict) else None
            allow_direct = bool(pol.get("allow_direct_call")) if isinstance(pol, dict) else False
            # workspace-write 一律禁止直调；其他模式也需显式白名单
            if mode == "workspace-write" or not allow_direct:
                if require_enforcement and sandbox._verdict() == "unusable":
                    return {"error": f"sandbox unavailable: {_FATAL_PREFIX}{_NOT_ENFORCED_MSG} (exit {LAUNCHER_FAILURE_EXIT})"}
                return {"error": f"tool_error: direct func call forbidden without allow_direct_call (mode={mode})"}
            return func(**args) if isinstance(args, dict) else func(args)
        return {"error": "tool_error: no cmd/argv and no callable func"}
    except SandboxUnavailableError as e:
        # fail-closed 但对工具层转为可观测的结构化 error
        return {"error": f"sandbox unavailable: {e}"}
    except (SandboxViolation, TypeError, subprocess.TimeoutExpired):
        # 编程错误与超时上浮，调用方必须感知而非误读 dict
        raise
