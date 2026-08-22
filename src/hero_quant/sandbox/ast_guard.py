"""L0 AST 安全守卫 — 白名单与黑名单深度扫描。

职责：对 LLM/用户生成的 Python 代码做静态 AST 审查，是沙箱的第一道防线。
安全设计：默认拒绝——白名单外一律拦截；黑名单（socket/subprocess/ctypes/
requests/os 等）优先于白名单；深层遍历捕获嵌套函数/类中的违规导入与调用。
白名单与 pyproject 依赖及 quantlib 扩展（joblib/duckdb 等）保持同步。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

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
    # 向上查找 pyproject.toml，兼容不同安装布局
    candidates = [
        Path(__file__).resolve().parents[3] / "pyproject.toml",  # src/hero_quant/sandbox -> repo root
        Path(__file__).resolve().parents[2] / "pyproject.toml",
        Path.cwd() / "pyproject.toml",
    ]
    pyproject = None
    for c in candidates:
        if c.exists():
            pyproject = c
            break
    if pyproject is None:
        return roots
    try:
        try:
            import tomllib  # type: ignore  # Python 3.11+ 标准库
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore  # 兼容低版本

        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:
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
_DYNAMIC_ROOTS = _load_pyproject_roots()
ALLOWED_ROOTS: set[str] = set(_STATIC_ALLOWED) | set(_DYNAMIC_ROOTS) | set(_QUANTLIB_EXTRA)

# 再次确保 quantlib 扩展始终存在
ALLOWED_ROOTS.update(_QUANTLIB_EXTRA)

# 显式黑名单：拦截可导致命令执行/网络外联/底层逃逸的根模块与调用
BANNED_IMPORT_ROOTS = {"socket", "subprocess", "ctypes", "requests", "os"}
BANNED_CALL_NAMES = {"eval", "exec", "__import__"}  # 动态执行与导入劫持
# 属性级黑名单：(base, attr)，防止通过 os.system 等间接执行
BANNED_ATTRS = {
    ("os", "system"),  # shell 命令执行
    ("os", "popen"),  # 管道执行
    ("os", "execve"),  # 进程替换
    ("os", "spawnl"),  # 进程派生
    ("os", "spawnlp"),
    ("subprocess", "Popen"),  # 子进程创建
    ("subprocess", "call"),
    ("subprocess", "run"),
    ("subprocess", "check_call"),
    ("subprocess", "check_output"),
}


def _is_banned_attribute(node: ast.Attribute) -> bool:
    """判定属性访问是否命中黑名单（直接执行能力或受限根模块的任意属性）。"""
    if isinstance(node.value, ast.Name):
        base = node.value.id
        attr = node.attr
        if (base, attr) in BANNED_ATTRS:
            return True
        # ctypes / socket / requests 的任意属性均视为高危（外联或底层操作）
        if base in {"ctypes", "socket", "requests"}:
            return True
        if base == "subprocess":
            return True  # subprocess 的任意方法均可能创建子进程
        if base == "os" and attr in {"system", "popen", "execve", "spawnl", "spawnlp", "execv", "execl"}:
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

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                # 黑名单优先于白名单
                if root in BANNED_IMPORT_ROOTS:
                    return False
                if root not in ALLOWED_ROOTS:
                    return False  # 默认拒绝：非白名单一律拦截
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                return False  # 相对导入无明确根，视为不安全
            root = node.module.split(".")[0]
            if root in BANNED_IMPORT_ROOTS:
                return False
            if root not in ALLOWED_ROOTS:
                return False
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BANNED_CALL_NAMES:
                return False  # 拦截 eval/exec/__import__ 动态执行
            if isinstance(func, ast.Attribute):
                if _is_banned_attribute(func):
                    return False
        elif isinstance(node, ast.Attribute):
            # 即使未调用，单纯引用高危属性也应拦截（如 x = os.system）
            if _is_banned_attribute(node):
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
    return set(ALLOWED_ROOTS)


def is_allowlist_synced_with_pyproject() -> tuple[bool, list[str]]:
    """检查白名单与 pyproject 的同步状态，返回 (是否同步, 缺失列表)。"""
    dynamic = _load_pyproject_roots()
    missing = [r for r in dynamic if r not in ALLOWED_ROOTS]
    return (len(missing) == 0, missing)
