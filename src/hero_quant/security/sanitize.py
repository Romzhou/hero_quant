"""输入净化 — ticker 路径组件校验，防目录穿越。

职责：校验外部输入的 ticker 是否可安全拼入文件路径，拦截 ``../`` 等穿越。
安全设计：白名单正则 ``^[A-Za-z0-9._\\-\\^=+]+$`` 且长度 ≤32，拒绝空值、
纯点号与非法字符；允许 ``^GSPC``/``GC=F`` 等合法符号，阻断路径逃逸。
"""

from __future__ import annotations

import re
from pathlib import Path

# 允许字符：字母/数字/点/中划线/下划线/尖号(^GSPC)/等号(GC=F)/加号(XAUUSD+)
# 以上均不具备目录穿越能力，超出此集合的字符一律拒绝
_TICKER_PATH_RE = re.compile(r"^[A-Za-z0-9._\-\^=+]+$")

# Windows 保留设备名（不区分大小写）与尾有点/空格会产生隐藏碰撞，Linux 部署亦应拒绝
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *{f"COM{i}" for i in range(1, 10)},
    *{f"LPT{i}" for i in range(1, 10)},
}


def safe_ticker_component(ticker: str, *, max_len: int = 32) -> str:
    """校验 ticker 是否可安全拼入文件路径；合法则原样返回，否则抛 ValueError。

    安全说明：本函数仅校验单一路径组件（不含 '/'/'\\'），调用方仍需用
    ``safe_join(base, ticker)`` 或自行 ``(Path(base)/ticker).resolve().is_relative_to(Path(base).resolve())``
    校验拼接后不逃逸 base 目录，避免字符串拼接 ``f"{base}/{ticker}"`` 的误用。
    """
    if not isinstance(max_len, int) or not 0 < max_len <= 255:
        raise ValueError(f"max_len must be int in (0, 255], got {max_len!r}")
    if not isinstance(ticker, str) or not ticker:
        raise ValueError(f"ticker must be a non-empty string, got {ticker!r}")
    if len(ticker) > max_len:
        raise ValueError(f"ticker exceeds {max_len} chars: {ticker!r}")
    # 中文：纯点号 ('.'/'..'/'...') 必须先于尾点检查，否则该分支不可达
    # （set(ticker)=={'.'} 蕴含 endswith('.')，后置即死代码）
    if set(ticker) == {"."}:
        raise ValueError(f"ticker cannot consist solely of dots: {ticker!r}")
    # 中文：尾点/空格校验必须在正则前，否则空格被正则拦截导致分支不可达
    if ticker.endswith(".") or ticker.endswith(" "):
        raise ValueError(f"ticker cannot end with dot or space: {ticker!r}")
    if not _TICKER_PATH_RE.fullmatch(ticker):
        raise ValueError(
            f"ticker contains characters not allowed in a filesystem path: {ticker!r}"
        )
    # 拒绝 Windows 保留设备名与尾有点/空格，避免跨平台隐藏文件/设备碰撞
    base_name = ticker.split(".")[0].upper()
    # 针对含扩展的如 CON.txt 亦需拒绝，按首段判断
    if base_name in _RESERVED_NAMES or ticker.upper() in _RESERVED_NAMES:
        raise ValueError(f"ticker is Windows reserved name: {ticker!r}")
    return ticker


def safe_join(base: str | Path, ticker: str, *, max_len: int = 32) -> Path:
    """安全拼接 base 与 ticker，确保结果仍在 base 目录内。

    校验流程：先经 safe_ticker_component 校验组件合法性，再用 Path.resolve() 做
    规范化并校验 ``is_relative_to``，防目录穿越与符号链接逃逸。
    """
    validated = safe_ticker_component(ticker, max_len=max_len)
    # 中文：空/CWD base（''/空白/'.'/None/非 str-Path/嵌 NUL）静默锚定 CWD
    #（Path('')==Path('.')），必须显式拒绝；str 与 Path 同一逻辑同一处理。
    # base 构造纳入归一化 try（含 TypeError），保证调用方只见到 ValueError。
    def _base_text(b: str | Path) -> str:
        return b.strip() if isinstance(b, str) else str(b).strip()
    if not isinstance(base, (str, Path)) or _base_text(base) in ("", "."):
        raise ValueError(f"invalid base path: {base!r}")
    if isinstance(base, str) and "\x00" in base:
        raise ValueError(f"invalid base path: {base!r}")
    # 中文：分拆 resolve 调用并窄化捕获，避免 broad Exception 吞没无关错误
    try:
        base_p = Path(base)
        target = (base_p / validated).resolve()
    except (OSError, ValueError, RuntimeError, TypeError) as e:
        raise ValueError(f"invalid base or ticker path: {e}") from e
    try:
        base_resolved = base_p.resolve()
    except (OSError, ValueError, RuntimeError, TypeError) as e:
        raise ValueError(f"invalid base or ticker path: {e}") from e
    # 中文：已存在的 file base 仍可通过 is_relative_to 前缀语义（/tmp/f 为 /tmp/f/AAPL 前缀），
    # 必须要求存在路径为目录，否则调用方的“锚定目录”假设被打破。
    # 校验 raise 置于 try 之外，避免自捕获重包误报根因；try 内仅包文件系统调用。
    try:
        is_file_base = base_resolved.exists() and not base_resolved.is_dir()
    except (OSError, RuntimeError) as e:
        raise ValueError(f"invalid base path: {e}") from e
    if is_file_base:
        raise ValueError(f"base must be a directory, got {base_resolved}")
    # is_relative_to 在 Python 3.9+ 可用；兼容处理
    try:
        if not target.is_relative_to(base_resolved):  # type: ignore[attr-defined]
            raise ValueError(f"path traversal detected: {target} not in {base_resolved}")
    except AttributeError:
        # fallback for older Python
        try:
            target.relative_to(base_resolved)
        except ValueError as e:
            raise ValueError(f"path traversal detected: {target} not in {base_resolved}") from e
    return target
