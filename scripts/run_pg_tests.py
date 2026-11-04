#!/usr/bin/env python3
"""本机 PG 守卫真跑：从环境/.env 解析 DSN（不回显），只跑 PG 集成族。

背景：`tests/conftest.py` 默认中和 `.env` 里的 DSN 键，保证离线用例确定性；
需要真 PG 的用例必须显式导出 DSN。本脚本把「显式导出」做成一条命令，
避免每次手拼 DSN，也避免把口令打进 shell history / 日志。

用法：
    python scripts/run_pg_tests.py                 # 默认 PG 族
    python scripts/run_pg_tests.py -k rls -x      # 追加 pytest 参数
    HERO_ENV_FILE=/path/to/prod.env python scripts/run_pg_tests.py

解析优先级：进程环境 > .env 文件；键顺序：
    HERO_CHECKPOINT_DSN > HERO_PG_DSN > HERO_PG_HOST/USER/PASSWORD 拼装
没有任何可用 DSN 时 exit 2，并列出尝试过的键名（不打印值）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PG_TESTS = (
    "tests/test_w4w8_pg.py",
    "tests/test_data_snapshot_pg.py",
    "tests/test_checkpoint_pg.py",
    "tests/test_j8_loop_ckpt_gov.py",
    # 批次14：审计链 / 长任务队列（真 PG）
    "tests/test_pr241_audit_pg.py",
    "tests/test_pr201_queue_pg.py",
    "tests/test_pr215_admin_pg.py",
    # 批次16：会话级事件入审计链
    "tests/test_pr241_session_pg.py",
)
_PG_PREFIXES = ("postgresql://", "postgres://", "postgresql+psycopg://")
_DSN_KEYS = ("HERO_CHECKPOINT_DSN", "HERO_PG_DSN")


def _dotenv_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        from dotenv import dotenv_values

        return {k: v for k, v in dotenv_values(path).items() if v is not None}
    except ImportError:
        # 最小环境兜底：只认简单 KEY=VALUE，不执行任何 shell 逻辑。
        values: dict[str, str] = {}
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
        return values


def _first(env: dict[str, str], key: str) -> str:
    return (os.environ.get(key) or env.get(key) or "").strip()


def _resolve_dsn(env: dict[str, str]) -> tuple[str, str]:
    """返回 (dsn, source)；source 只用于提示，不含 DSN 值。"""
    for key in _DSN_KEYS:
        value = _first(env, key)
        if value.startswith(_PG_PREFIXES):
            return value, key
    host = _first(env, "HERO_PG_HOST")
    user = _first(env, "HERO_PG_USER")
    password = os.environ.get("HERO_PG_PASSWORD") or env.get("HERO_PG_PASSWORD") or ""
    if host:
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if user else ""
        return f"postgresql://{auth}{host}:5432/heroquant", "HERO_PG_HOST/USER/PASSWORD"
    return "", ""


def _env_file() -> Path:
    """定位 env 文件：显式 HERO_ENV_FILE > 当前 worktree/.env > 主仓/.env（worktree 嵌套时）。"""
    explicit = os.environ.get("HERO_ENV_FILE")
    if explicit:
        return Path(explicit)
    candidates = [_REPO_ROOT / ".env"]
    try:
        candidates.append(_REPO_ROOT.parents[2] / ".env")
    except IndexError:  # pragma: no cover - 极简目录深度
        pass
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    env_file = _env_file()
    env = _dotenv_values(env_file)
    dsn, source = _resolve_dsn(env)
    if not dsn:
        tried = ", ".join(_DSN_KEYS) + ", HERO_PG_HOST/USER/PASSWORD"
        print(f"no usable PostgreSQL DSN (env file: {env_file.name}); tried: {tried}", file=sys.stderr)
        return 2

    print(f"PG DSN resolved from {source} (value hidden)")
    # 显式给出 .py 路径时只跑这些文件；其余参数（-k/-x/--maxfail 等）原样透传。
    explicit = [arg for arg in args if arg.endswith(".py")]
    flags = [arg for arg in args if not arg.endswith(".py")]
    tests = tuple(explicit) if explicit else _PG_TESTS
    print(f"running: {' '.join(tests)}")
    child_env = os.environ.copy()
    child_env["HERO_CHECKPOINT_DSN"] = dsn
    command = [sys.executable, "-m", "pytest", *tests, "-q", "-rs", *flags]
    return subprocess.call(command, cwd=str(_REPO_ROOT), env=child_env)


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
