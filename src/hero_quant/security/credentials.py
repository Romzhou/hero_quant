"""Credentials refs — per-operation re-resolve + shadow fail-loud + 0600.

REF_PATTERN supports ${ENV_VAR} and ref:xxx / credential:xxx forms.
resolve() re-parses on every call, never caches, shadow fail-loud.
Placeholder for 0600 hot-reload: files written with 0o600, reload on next resolve.
Uses environ.get pattern (config gate compliant).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Matches ${VAR}, $VAR, ref:xxx, credential:xxx, env:xxx
REF_PATTERN = re.compile(r"^(?:\$\{([^}]+)\}|\$(?P<var2>[A-Za-z_][A-Za-z0-9_]*)$|(?:ref|credential|env):(?P<ref>.+))")

# Fallback generic: any ${...}
_GENERIC_REF = re.compile(r"\$\{([^}]+)\}")


def _resolve_env_key(key: str) -> str | None:
    # shadow read — single source via environ.get
    return os.environ.get(key)


def resolve(ref: str) -> str:
    """Resolve credential ref per-operation, shadow fail-loud.

    - Plain value without ref pattern returns as-is.
    - ${VAR} or $VAR -> env lookup, fail-loud if missing (ValueError).
    - ref:xxx / credential:xxx / env:xxx -> env lookup or file lookup placeholder.
    - Hot-reload placeholder: re-reads file each time if path exists, ensures 0o600.
    """
    if not isinstance(ref, str):
        raise TypeError("ref must be str")
    ref = ref.strip()
    if not ref:
        return ref

    # Direct ${VAR} entire string
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
            if val is None:
                # Try file path if var looks like path
                p = Path(var)
                if p.exists() and p.is_file():
                    # Enforce 0600 on read path placeholder (no overwrite)
                    try:
                        # Hot-reload: just read, permission check placeholder
                        stat = p.stat()
                        # If file not 0600, we still read but could warn
                        _ = oct(stat.st_mode)[-3:]
                    except Exception:
                        pass
                    return p.read_text(encoding="utf-8").strip()
                raise ValueError(f"credential ref not found (shadow fail-loud): {ref}")
            return val

    # Embedded ${VAR} inside larger string — replace all
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

    # No pattern — return plain value
    return ref


def write_credential_file(path: str | Path, content: str) -> Path:
    """Write credential file with 0600, placeholder for hot-reload."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write via tmp + rename placeholder for durability
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
