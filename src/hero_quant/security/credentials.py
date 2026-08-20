"""Credentials refs — per-operation re-resolve + shadow fail-loud + 0600.

REF_PATTERN supports ${ENV_VAR} and ref:xxx / credential:xxx forms.
resolve() re-parses on every call, never caches, shadow fail-loud.
Hot-reload: files re-read on each resolve; 0600 enforcement via os.stat warns if not 0600.
Uses environ.get pattern (config gate compliant).
"""

from __future__ import annotations

import os
import re
import warnings
from pathlib import Path

# Matches ${VAR}, $VAR, ref:xxx, credential:xxx, env:xxx
REF_PATTERN = re.compile(r"^(?:\$\{([^}]+)\}|\$(?P<var2>[A-Za-z_][A-Za-z0-9_]*)$|(?:ref|credential|env):(?P<ref>.+))")

# Fallback generic: any ${...}
_GENERIC_REF = re.compile(r"\$\{([^}]+)\}")


def _check_0600(path: Path) -> None:
    """Warn if file permissions are not 0600 (owner read/write only)."""
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
        # stat may fail on Windows or missing; do not block resolve
        pass


def _read_credential_file(path: Path) -> str:
    """Read credential file with 0600 check and hot-reload (re-read each call)."""
    _check_0600(path)
    return path.read_text(encoding="utf-8").strip()


def _resolve_env_key(key: str) -> str | None:
    # shadow read — single source via environ.get
    return os.environ.get(key)


def resolve(ref: str) -> str:
    """Resolve credential ref per-operation, shadow fail-loud.

    - Plain value without ref pattern returns as-is (unless path exists -> file).
    - ${VAR} or $VAR -> env lookup, fail-loud if missing (ValueError).
    - ref:xxx / credential:xxx / env:xxx -> env lookup or file lookup.
    - Hot-reload: re-reads file each time if path exists, warns if not 0600.
    - Plain file path that exists -> read with 0600 check (hot-reload).
    """
    if not isinstance(ref, str):
        raise TypeError("ref must be str")
    ref = ref.strip()
    if not ref:
        return ref

    # Direct ${VAR} entire string or ref:xxx
    m = REF_PATTERN.match(ref)
    if m:
        # ${VAR}
        var = m.group(1) or m.group("var2") or m.group("ref")
        if var:
            var = var.strip()
            # Support ${VAR:-default} minimal
            if ":-" in var:
                key, default = var.split(":-", 1)
                val = _resolve_env_key(key.strip())
                if val is None:
                    return default
                return val
            val = _resolve_env_key(var)
            if val is not None:
                # If env value is a file path that exists, hot-reload file content?
                # Spec: reads env OR file path each call — if env points to file, prefer env value.
                # But also support var being a file path when env missing:
                return val
            # Not in env — try file path if var looks like path
            p = Path(var)
            # Also try expanded user / env path
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
            # Also handle ref:env:VAR where env var value is file path
            # Fallback fail-loud
            raise ValueError(f"credential ref not found (shadow fail-loud): {ref}")

    # Embedded ${VAR} inside larger string — replace all (re-parse each call)
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

    # No pattern — plain value. Hot-reload: if it's a file path that exists, read it
    # (supports direct file ref without ref: prefix; re-read each resolve)
    try:
        p_plain = Path(ref)
        if p_plain.exists() and p_plain.is_file():
            # Avoid reading huge arbitrary files: only if path looks credential-like
            # or caller explicitly passes file path. We treat any existing file as credential file.
            return _read_credential_file(p_plain)
    except Exception:
        pass

    # Return plain value as-is
    return ref


def write_credential_file(path: str | Path, content: str) -> Path:
    """Write credential file with 0600, placeholder for hot-reload."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write via tmp + rename for durability
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    tmp.replace(p)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    return p
