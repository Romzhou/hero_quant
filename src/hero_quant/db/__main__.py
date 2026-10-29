"""`python -m hero_quant.db <status|upgrade|check>` — 迁移运维入口。

退出码：0 成功一致；1 drift / 待应用（check）；2 配置或连接失败。
禁止在日志/输出中回显 DSN 口令。
"""

from __future__ import annotations

import json
import sys

from hero_quant.db.loader import MigrationLoadError
from hero_quant.db.runner import MigrationError, apply_migrations, drift_report, status

_USAGE = "usage: python -m hero_quant.db {status|upgrade|check}"


def _conninfo(dsn: str) -> str:
    """psycopg 只认 postgresql://；去掉 SQLAlchemy 风格驱动后缀。"""
    return dsn.replace("postgresql+psycopg://", "postgresql://", 1).replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in {"status", "upgrade", "check"}:
        print(_USAGE, file=sys.stderr)
        return 2
    command = args[0]

    try:
        from hero_quant.config.settings import get_settings

        dsn = get_settings().checkpoint_dsn
    except Exception as exc:  # noqa: BLE001 - 配置层失败统一为退出码 2
        print(f"settings unavailable: {type(exc).__name__}", file=sys.stderr)
        return 2
    if not isinstance(dsn, str) or not dsn.strip():
        print("checkpoint DSN not configured (HERO_CHECKPOINT_DSN / HERO_PG_DSN)", file=sys.stderr)
        return 2

    try:
        import psycopg  # noqa: PLC0415 - CLI 按需导入，避免模块导入期硬依赖
    except ImportError:
        print("psycopg is not installed", file=sys.stderr)
        return 2

    try:
        conn = psycopg.connect(_conninfo(dsn.strip()), connect_timeout=5)
    except Exception as exc:  # noqa: BLE001 - 连接失败只报类型，绝不回显 DSN
        print(f"database connection failed: {type(exc).__name__}", file=sys.stderr)
        return 2

    try:
        if command == "upgrade":
            applied = apply_migrations(conn)
            print(json.dumps({"applied": applied}, ensure_ascii=False))
            return 0

        if command == "status":
            print(json.dumps(status(conn), ensure_ascii=False, default=str))
            return 0

        problems = drift_report(conn)
        if problems:
            print(json.dumps({"drift": problems}, ensure_ascii=False))
            return 1
        print(json.dumps({"drift": []}, ensure_ascii=False))
        return 0
    except (MigrationError, MigrationLoadError) as exc:
        print(f"migration error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
