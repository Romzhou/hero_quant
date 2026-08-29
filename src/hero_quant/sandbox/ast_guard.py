"""L0 AST 安全守卫 — 白名单与黑名单深度扫描。

职责：对 LLM/用户生成的 Python 代码做静态 AST 审查，是沙箱的第一道防线。
安全设计：默认拒绝——白名单外一律拦截；黑名单（socket/subprocess/ctypes/
requests/os 等）优先于白名单；深层遍历捕获嵌套函数/类中的违规导入与调用。
白名单与 pyproject 依赖及 quantlib 扩展（joblib/duckdb 等）保持同步。
"""
from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 基础白名单 —— 基于 pyproject.toml 与 quantlib 扩展手工同步，保持显式可审计
# 动态加载器会在导入时补充，避免文件漂移导致遗漏
# ---------------------------------------------------------------------------
_STATIC_ALLOWED = {
    # 核心科学计算（量化主依赖）
    "pandas",
    "numpy",
    "scipy",
    "math",
    "typing",
    # 项目依赖的导入根
    "fastapi",
    "uvicorn",
    "pydantic",
    "dotenv",  # python-dotenv
    "httpx",
    "rich",
    "yaml",  # pyyaml
    "langchain",
    "langchain_openai",  # langchain-openai
    "langchain_core",
    "prometheus_client",
    "structlog",
    # 可选数据源
    "tushare",
    "akshare",
    "yfinance",
    "ccxt",
    "polars",
    # 开发期辅助（生成代码可能引用，非安全敏感）
    "pytest",
    "pytest_cov",
    "ruff",
    "black",
    # quantlib 扩展
    "joblib",
    "duckdb",
    "sklearn",
    "statsmodels",
    "pyarrow",
    "numba",
    # 标准库中对量化常用的安全辅助模块
    "json",
    "re",
    "datetime",
    "collections",
    "itertools",
    "functools",
    "statistics",
    "decimal",
    "hashlib",
    "enum",
    "dataclasses",
    "pathlib",
    "logging",
    "copy",
    "operator",
    "string",
    "uuid",
    "time",
    "calendar",
    "zoneinfo",
}

# quantlib 扩展集合——确保即使 pyproject 未声明也保持可用
_QUANTLIB_EXTRA = {"joblib", "duckdb", "sklearn", "statsmodels", "pyarrow", "polars", "numba"}

# ---------------------------------------------------------------------------
# 发行包名 -> 导入根映射（统一小写处理，兼容 hyphen/underscore 差异）
# ---------------------------------------------------------------------------
_DIST_ALIAS: dict[str, str] = {
    "python-dotenv": "dotenv",
    "pyyaml": "yaml",
    "prometheus_client": "prometheus_client",
    "prometheus-client": "prometheus_client",
    "langchain-openai": "langchain_openai",
    "langchain_core": "langchain_core",
    "langchain-core": "langchain_core",
    "scikit-learn": "sklearn",
    "pytest-cov": "pytest_cov",
}


def _dist_to_import(dist: str) -> str:
    """将发行包名归一化为导入根（如 python-dotenv -> dotenv）。"""
    d = dist.strip().lower()
    if d in _DIST_ALIAS:
        return _DIST_ALIAS[d]
    # 默认以 hyphen 转 underscore 作为导入映射
    return d.replace("-", "_")


def _load_pyproject_roots() -> set[str]:
    """解析 pyproject.toml 依赖并返回导入根集合（尽力而为，失败返回空集）。"""
    roots: set[str] = set()
    # 向上迭代 parents 直到根，兼容不同安装布局
    pyproject = None
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "pyproject.toml"
        if candidate.is_file():
            pyproject = candidate
            break
    if pyproject is None:
        cwd_candidate = Path.cwd() / "pyproject.toml"
        if cwd_candidate.is_file():
            pyproject = cwd_candidate
    if pyproject is None:
        return roots
    try:
        try:
            import tomllib  # type: ignore  # Python 3.11+ 标准库
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore  # 兼容低版本

        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        # 窄化为 OSError / TOMLDecodeError（ValueError 覆盖 TOMLDecodeError）
        logger.warning("failed to load pyproject %s: %s", pyproject, e)
        return roots
    deps: list[str] = []
    deps.extend(data.get("project", {}).get("dependencies", []) or [])
    for group in (data.get("project", {}).get("optional-dependencies", {}) or {}).values():
        deps.extend(group)
    for raw in deps:
        # 去除环境标记与扩展： "uvicorn[standard]>=0.24 ; ..." -> "uvicorn"
        base = raw.strip().split(";")[0].strip()
        base = re.split(r"\[", base, maxsplit=1)[0]  # 去除 [extra]
        base = re.split(r"[<>=!~]", base, maxsplit=1)[0].strip().lower()  # 去除版本约束
        if not base:
            continue
        roots.add(_dist_to_import(base))
    return roots


# 静态与动态白名单取并集，保证与 pyproject 同步且不因漂移丢失条目
# 懒加载 _DYNAMIC_ROOTS 通过 _get_allowed_roots() 按需初始化，避免导入时 I/O 副作用
_DYNAMIC_ROOTS: set[str] | None = None


def _get_dynamic_roots() -> set[str]:
    """懒加载动态根集合，首次调用时解析 pyproject。"""
    global _DYNAMIC_ROOTS
    if _DYNAMIC_ROOTS is None:
        _DYNAMIC_ROOTS = _load_pyproject_roots()
    return _DYNAMIC_ROOTS


def _get_allowed_roots() -> set[str]:
    """返回完整的白名单集合（静态+动态+扩展），用于懒加载初始化。"""
    return set(_STATIC_ALLOWED) | set(_QUANTLIB_EXTRA) | set(_get_dynamic_roots())


_STATIC_ROOTS: set[str] = set(_STATIC_ALLOWED) | set(_QUANTLIB_EXTRA)
# 导入时仅静态可审计集合；动态 roots 首次 via _get_allowed_roots() 懒合并（避免顶层 I/O 副作用）
# 对外仍暴露 ALLOWED_ROOTS 变量，校验走 _get_allowed_roots() / get_allowed_roots() 懒合并
ALLOWED_ROOTS: set[str] = set(_STATIC_ROOTS)
_HAS_DYNAMIC_ATTR = False  # 标记是否已动态合并，避免 __getattr__ 与变量共存冲突


def reload_allowed_roots() -> set[str]:
    """按需将 pyproject 动态 roots 合并进 ALLOWED_ROOTS 并返回拷贝（幂等）。"""
    global ALLOWED_ROOTS, _HAS_DYNAMIC_ATTR
    merged = _get_allowed_roots()
    ALLOWED_ROOTS = set(merged)
    _HAS_DYNAMIC_ATTR = True
    return set(ALLOWED_ROOTS)

# 显式黑名单：拦截可导致命令执行/网络外联/底层逃逸的根模块与调用
# kept only dangerous roots; pathlib/shutil removed — they are in _STATIC_ALLOWED and safe for quant I/O
BANNED_IMPORT_ROOTS = {
    "socket",
    "subprocess",
    "ctypes",
    "requests",
    "os",
    "sys",
    "importlib",
    "importlib.util",
    "importlib.machinery",
    "importlib.abc",
    "io",
    "builtins",
    "__builtins__",
    "posix",
    "_io",
    "_socket",
    "pty",
    "multiprocessing",
    "threading",
    "signal",
}
BANNED_CALL_NAMES = {"eval", "exec", "__import__", "compile", "open", "breakpoint"}  # 动态执行与导入劫持
BANNED_GETATTR_NAMES = {"getattr", "setattr", "hasattr", "vars", "getattribute", "__getattribute__"}
# 属性级危险 dunder - 用于 getattr 第二参数及直接属性访问检测
BANNED_DUNDER_ATTRS = {
    "__class__",
    "__bases__",
    "__subclasses__",
    "__mro__",
    "__dict__",
    "__globals__",
    "__code__",
    "__import__",
    "__builtins__",
    "__getattribute__",
    "__subclasscheck__",
}
# 属性级黑名单：(base, attr)，防止通过 os.system 等间接执行
BANNED_ATTRS = {
    ("os", "system"),
    ("os", "popen"),
    ("os", "execve"),
    ("os", "execv"),
    ("os", "execl"),
    ("os", "spawnl"),
    ("os", "spawnlp"),
    ("os", "fork"),
    ("os", "kill"),
    ("subprocess", "Popen"),
    ("subprocess", "call"),
    ("subprocess", "run"),
    ("subprocess", "check_call"),
    ("subprocess", "check_output"),
}


def _is_banned_subscript(node: ast.Subscript) -> bool:
    """检测 Subscript 索引为危险 dunder 常量字符串（如 obj['__class__']）。"""
    slc = node.slice
    if isinstance(slc, ast.Constant) and isinstance(slc.value, str) and slc.value in BANNED_DUNDER_ATTRS:
        return True
    return False


def _get_root_name(node: ast.AST) -> str | None:
    """递归剥离 Attribute/Call/Subscript 直到 Name，返回根名称或 None（含 dunder 下标透传）。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        # __getattribute__ 本身即高危，透传为 dunder 信号
        if node.attr in BANNED_DUNDER_ATTRS:
            return f"__dunder__{node.attr}"
        return _get_root_name(node.value)
    if isinstance(node, ast.Call):
        return _get_root_name(node.func)
    if isinstance(node, ast.Subscript):
        if _is_banned_subscript(node):
            # 下标为 __class__ 等时视为 dunder 访问，直接拦截
            return f"__dunder__{node.slice.value}"  # type: ignore[attr-defined]
        return _get_root_name(node.value)
    return None


def _is_banned_attribute(node: ast.Attribute, alias_map: dict[str, str] | None = None) -> bool:
    """判定属性访问是否命中黑名单（直接执行能力或受限根模块的任意属性）。

    支持链式属性（a.b.c）与别名映射：通过 _get_root_name 提取根，
    再经 alias_map 还原到真实根后判定 BANNED_IMPORT_ROOTS。
    同时拦截危险 dunder 属性 (如 __class__/__bases__) 的直接访问，
    含 getattr 家族与 __getattribute__。
    """
    alias_map = alias_map or {}
    attr = node.attr
    # 危险 dunder 属性无视根，直接拦截；含 __getattribute__
    if attr in BANNED_DUNDER_ATTRS:
        return True
    if attr in BANNED_GETATTR_NAMES:
        return True
    root = _get_root_name(node)
    if root is None:
        return False
    # _get_root_name 对 dunder 下标/属性已透传为 __dunder__*，此处一律拦截
    if isinstance(root, str) and root.startswith("__dunder__"):
        return True
    effective = alias_map.get(root, root)
    if (effective, attr) in BANNED_ATTRS:
        return True
    # 受限根的任意属性均视为高危；链式解析后命中 banned root 即拦截
    if effective in BANNED_IMPORT_ROOTS:
        return True
    if effective in {"ctypes", "socket", "requests"}:
        return True
    if effective == "subprocess":
        return True
    if effective == "os" and attr in {"system", "popen", "execve", "spawnl", "spawnlp", "execv", "execl"}:
        return True
    return False


def check_import_allowlist(code: str) -> bool:
    """检查代码是否仅使用白名单导入且无黑名单模式；深层遍历捕获嵌套作用域。"""
    if not code or not code.strip():
        return True
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    # 收集别名映射 {asname -> real_root}，用于链式与别名绕过检测
    alias_map: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                asname = alias.asname if alias.asname else alias.name.split(".")[0]
                alias_map[asname] = root
                # 处理 `import os.path` 无别名时，补 root 自映射
                if alias.asname is None and "." in alias.name:
                    alias_map[root] = root
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            root = node.module.split(".")[0]
            for alias in node.names:
                asname = alias.asname if alias.asname else alias.name
                alias_map[asname] = root

    def _is_banned_module(mod: str) -> bool:
        # 检查完整模块名或根是否命中黑名单（支持 importlib.util 这类点分黑名单）
        if mod in BANNED_IMPORT_ROOTS:
            return True
        root = mod.split(".")[0]
        if root in BANNED_IMPORT_ROOTS:
            return True
        # 前缀匹配：importlib.util 命中 importlib.util.xxx
        for b in BANNED_IMPORT_ROOTS:
            if mod == b or mod.startswith(b + "."):
                return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                full = alias.name
                root = full.split(".")[0]
                # 黑名单优先于白名单（完整匹配+前缀）
                if _is_banned_module(full):
                    return False
                if root not in _get_allowed_roots():
                    return False  # 默认拒绝：非白名单一律拦截（懒合并动态 roots）
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                return False  # 相对导入无明确根，视为不安全
            mod = node.module
            if _is_banned_module(mod):
                return False
            root = mod.split(".")[0]
            if root not in _get_allowed_roots():
                return False
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BANNED_CALL_NAMES:
                return False  # 拦截 eval/exec/__import__/compile/open/breakpoint 等
            if isinstance(func, ast.Name) and func.id in BANNED_GETATTR_NAMES:
                # getattr 家族一律拦截；额外防御：若参数含 dunder 字符串也拦截
                # 已在上层直接 return False，此处保留参数检查以便未来细粒度放行时仍能拦截 getattr(x,"__class__")
                has_dunder_arg = False
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value in BANNED_DUNDER_ATTRS:
                        has_dunder_arg = True
                    root = _get_root_name(arg)
                    if root is not None:
                        effective = alias_map.get(root, root)
                        if effective in BANNED_IMPORT_ROOTS:
                            return False
                if has_dunder_arg:
                    return False
                return False
            # from-import 别名直接调用：`from os import system as s; s(...)`
            if isinstance(func, ast.Name) and func.id in alias_map:
                effective = alias_map.get(func.id, func.id)
                if effective in BANNED_IMPORT_ROOTS:
                    return False
            if isinstance(func, ast.Attribute):
                if _is_banned_attribute(func, alias_map):
                    return False
        elif isinstance(node, ast.Attribute):
            # 即使未调用，单纯引用高危属性也应拦截（如 x = os.system）
            if _is_banned_attribute(node, alias_map):
                return False
        elif isinstance(node, ast.Subscript):
            if _is_banned_subscript(node):
                return False
            # 链式下标后仍可能挂属性（如 obj["__class__"].__bases__），交由 Attribute 分支捕获
            root = _get_root_name(node)
            if isinstance(root, str) and root.startswith("__dunder__"):
                return False

    return True


def assert_allowlist(code: str) -> None:
    """断言代码通过白名单校验，否则抛 ValueError。"""
    if not check_import_allowlist(code):
        raise ValueError("import allowlist violation or banned pattern detected")


class SandboxViolation(RuntimeError):
    """AST 守卫违规：Python 分支 fail-closed 拒绝执行时抛出。"""


def check_source(source: str) -> None:
    """审查 Python 源码，违规或语法错误即抛 SandboxViolation（fail-closed）。

    约束：仅 Python 执行分支在 compile/exec 前调用；AST 解析失败必须拒绝；
    非 Python 载荷不受此函数影响。
    """
    if not source or not source.strip():
        return
    try:
        ast.parse(source)
    except SyntaxError as e:
        raise SandboxViolation(f"syntax error: {e}") from e
    if not check_import_allowlist(source):
        raise SandboxViolation("import allowlist violation or banned pattern detected")


def get_allowed_roots() -> set[str]:
    """返回当前白名单的拷贝，供测试与自检使用。"""
    return set(_get_allowed_roots())


def is_allowlist_synced_with_pyproject() -> tuple[bool, list[str]]:
    """检查白名单与 pyproject 的同步状态，返回 (是否同步, 缺失列表)。"""
    dynamic = _load_pyproject_roots()
    expected = set(_STATIC_ALLOWED) | set(_QUANTLIB_EXTRA)
    missing = [r for r in dynamic if r not in expected]
    return (len(missing) == 0, missing)
