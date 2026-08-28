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

# 匹配 ${VAR}、$VAR、ref:xxx / credential:xxx / env:xxx 三类引用 — 每分支均 $ 锚定，防前缀截断
REF_PATTERN = re.compile(r"^(?:\$\{(?P<braced>[^}]+)\}$|\$(?P<var2>[A-Za-z_][A-Za-z0-9_]*)$|(?:(?P<kind>ref|credential|env):(?P<ref>.+))$)")

# 兜底匹配：字符串内嵌的任意 ${...}
_GENERIC_REF = re.compile(r"\$\{([^}]+)\}")


def _check_0600(path: Path, *, strict: bool = False) -> None:
    """检查文件权限是否为 0600，非 0600 时告警（防多用户可读导致泄露）。

    strict=False 保持历史 warn-only 兼容；strict=True 时抛 PermissionError 强制隔离。
    仅捕获 OSError/FileNotFoundError，不吞噬其它异常，避免隐藏 Windows ACL 等错误。
    """
    try:
        st = os.stat(path)
        mode = st.st_mode & 0o777
        if mode != 0o600:
            msg = f"credential file {path} permissions {oct(mode)} not 0600"
            if strict:
                raise PermissionError(msg)
            warnings.warn(msg, UserWarning, stacklevel=3)
    except FileNotFoundError:
        return
    except OSError as e:
        if strict:
            raise ValueError(f"cannot stat credential file {path}: {e}") from e
        warnings.warn(f"cannot stat {path}: {e}", UserWarning, stacklevel=3)
        return


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

    # 匹配整串为 ${VAR} 或 ref:xxx 的情况 — 用 fullmatch 防前缀截断
    m = REF_PATTERN.fullmatch(ref)
    if m:
        # 兼容 braced 命名与旧未命名分组
        var = m.group("braced") or m.group(1) or m.group("var2") or m.group("ref")
        kind = m.groupdict().get("kind")
        if var:
            var = var.strip()
            # 支持 ${VAR:-default} 语义：unset 或空字符串均回落到 default（与 shell :- 一致）
            if ":-" in var:
                key, default = var.split(":-", 1)
                val = _resolve_env_key(key.strip())
                if not val:  # None or "" -> fallback
                    return default
                return val
            val = _resolve_env_key(var)
            if val is not None and val != "":
                # 环境变量存在且非空时直接返回，不再做文件二次解析
                return val
            # 处理 env: 前缀：显式要求仅读环境，不回落文件，fail-loud
            if kind == "env":
                raise ValueError(f"credential ref not found (shadow fail-loud): {ref}")
            # 对 ref:/credential:/${} / $VAR 允许文件回落，但不展开 env vars 防注入
            p = Path(var)
            # 仅用字面路径，不做 expandvars/expanduser，避免 $HOME 注入任意文件读取
            cand = p
            exists = False
            try:
                exists = cand.exists() and cand.is_file()
            except OSError:
                exists = False
            if exists:
                return _read_credential_file(cand)  # 让读取错误透出，不吞 PermissionError/UnicodeError
            # 未找到则 fail-loud
            raise ValueError(f"credential ref not found (shadow fail-loud): {ref}")

    # 字符串内嵌多个 ${VAR} 的替换（每次调用重新解析）
    if "${" in ref:
        def _repl(g):
            key = g.group(1).strip()
            if ":-" in key:
                k, default = key.split(":-", 1)
                v = _resolve_env_key(k.strip())
                return v if v is not None and v != "" else default
            v = _resolve_env_key(key)
            if v is None or v == "":
                raise ValueError(f"credential ref not found: {key}")
            return v

        return _GENERIC_REF.sub(_repl, ref)

    # 无模式的纯值：若指向已存在文件则按凭据文件读取（支持热重载）— 仅捕获 OSError，不吞读取错误
    p_plain = Path(ref)
    exists_plain = False
    try:
        exists_plain = p_plain.exists() and p_plain.is_file()
    except OSError:
        exists_plain = False
    if exists_plain:
        return _read_credential_file(p_plain)

    return ref


def write_credential_file(path: str | Path, content: str) -> Path:
    """写入凭据文件并置为 0600 权限，通过临时文件重命名保证原子性。"""
    import tempfile

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # use atomic temp with random suffix in same dir, mode 0o600, no world-readable window
    # handle multi-suffix correctly via p.name prefix, not with_suffix
    fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".tmp.")
    tmp = Path(tmp_path)
    try:
        try:
            os.fchmod(fd, 0o600)
        except Exception:
            try:
                os.chmod(tmp_path, 0o600)
            except Exception:
                pass
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        try:
            os.close(fd)
        except Exception:
            pass
    # ensure tmp is 0600 before replace (in case fchmod not available)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    try:
        # atomic replace
        tmp.replace(p)
    except Exception:
        # cleanup tmp on failure
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    try:
        os.chmod(p, 0o600)  # 确保最终文件仅所有者可读写
    except Exception:
        pass
    return p
