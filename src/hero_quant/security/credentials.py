"""凭据管理 — 按次解析、热重载与 0600 权限约束。

职责：为 ``${VAR}``/``ref:``/``credential:`` 等引用提供统一解析入口。
安全设计：每次调用重新解析不缓存；缺失时 fail-loud 抛异常；文件凭据
每次重读并以 ``os.stat`` 检查 0600 权限，非 0600 时告警，防凭据泄露与陈旧值复用。
"""

from __future__ import annotations

import os
import re
import warnings
from pathlib import Path

# 匹配 ${VAR}、$VAR、ref:xxx / credential:xxx / env:xxx 三类引用
REF_PATTERN = re.compile(r"^(?:\$\{([^}]+)\}|\$(?P<var2>[A-Za-z_][A-Za-z0-9_]*)$|(?:ref|credential|env):(?P<ref>.+))")

# 兜底匹配：字符串内嵌的任意 ${...}
_GENERIC_REF = re.compile(r"\$\{([^}]+)\}")


def _check_0600(path: Path) -> None:
    """检查文件权限是否为 0600，非 0600 时告警（防多用户可读导致泄露）。"""
    try:
        st = os.stat(path)
        mode = st.st_mode & 0o777
        if mode != 0o600:
            warnings.warn(
                f"credential file {path} permissions {oct(mode)} not 0600",
                UserWarning,
                stacklevel=3,
            )
    except FileNotFoundError:
        pass
    except Exception:
        # Windows 或缺失时 stat 可能失败，不阻塞解析流程
        pass


def _read_credential_file(path: Path) -> str:
    """读取凭据文件：先做 0600 检查，每次调用均重读以支持热重载。"""
    _check_0600(path)
    return path.read_text(encoding="utf-8").strip()


def _resolve_env_key(key: str) -> str | None:
    # 统一经 environ.get 读取，便于审计与 mock
    return os.environ.get(key)


def resolve(ref: str) -> str:
    """按次解析凭据引用，缺失时 fail-loud；文件凭据每次重读并校验 0600。"""
    if not isinstance(ref, str):
        raise TypeError("ref must be str")
    ref = ref.strip()
    if not ref:
        return ref

    # 匹配整串为 ${VAR} 或 ref:xxx 的情况
    m = REF_PATTERN.match(ref)
    if m:
        var = m.group(1) or m.group("var2") or m.group("ref")
        if var:
            var = var.strip()
            # 支持 ${VAR:-default} 语法
            if ":-" in var:
                key, default = var.split(":-", 1)
                val = _resolve_env_key(key.strip())
                if val is None:
                    return default
                return val
            val = _resolve_env_key(var)
            if val is not None:
                # 环境变量存在时直接返回其值，不再做文件二次解析
                return val
            # 环境变量缺失时尝试按文件路径读取
            p = Path(var)
            try:
                p_exp = Path(os.path.expandvars(os.path.expanduser(var)))
            except Exception:
                p_exp = p
            for cand in (p, p_exp):
                try:
                    if cand.exists() and cand.is_file():
                        return _read_credential_file(cand)
                except Exception:
                    continue
            # 未找到则 fail-loud，防静默使用空值
            raise ValueError(f"credential ref not found (shadow fail-loud): {ref}")

    # 字符串内嵌多个 ${VAR} 的替换（每次调用重新解析）
    if "${" in ref:
        def _repl(g):
            key = g.group(1).strip()
            if ":-" in key:
                k, default = key.split(":-", 1)
                v = _resolve_env_key(k.strip())
                return v if v is not None else default
            v = _resolve_env_key(key)
            if v is None:
                raise ValueError(f"credential ref not found: {key}")
            return v

        return _GENERIC_REF.sub(_repl, ref)

    # 无模式的纯值：若指向已存在文件则按凭据文件读取（支持热重载）
    try:
        p_plain = Path(ref)
        if p_plain.exists() and p_plain.is_file():
            return _read_credential_file(p_plain)
    except Exception:
        pass

    return ref


def write_credential_file(path: str | Path, content: str) -> Path:
    """写入凭据文件并置为 0600 权限，通过临时文件重命名保证原子性。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")  # 先写临时文件再原子替换，防并发截断
    tmp.write_text(content, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    tmp.replace(p)
    try:
        os.chmod(p, 0o600)  # 确保最终文件仅所有者可读写
    except Exception:
        pass
    return p
