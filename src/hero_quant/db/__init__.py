"""db — 版本化 schema 迁移与共享 PG 连接池（W4 PR-133）。

新路径 `db/migrations/` 是 schema 的唯一来源；仓库根历史 `migrations/` 保持冻结只读。
运行：`python -m hero_quant.db status|upgrade|check`。

前提：checkpoint 与 billing 共用同一库（docker-compose 默认同为 heroquant）。
若拆成独立 billing DSN，需对 billing 库单独执行 baseline（当前 CLI 只读
`Settings.checkpoint_dsn`）。
"""

from hero_quant.db.loader import Migration, MigrationLoadError, load_migrations
from hero_quant.db.pool import DatabaseUnavailable, close_pools, create_pool, get_pool
from hero_quant.db.runner import (
    EXPECTED_TABLES,
    MigrationDriftError,
    MigrationError,
    apply_migrations,
    applied_versions,
    current_version,
    drift_report,
    ensure_schema_version,
    pending_migrations,
    status,
)

__all__ = [
    "EXPECTED_TABLES",
    "DatabaseUnavailable",
    "Migration",
    "MigrationDriftError",
    "MigrationError",
    "MigrationLoadError",
    "apply_migrations",
    "applied_versions",
    "close_pools",
    "create_pool",
    "current_version",
    "drift_report",
    "ensure_schema_version",
    "get_pool",
    "load_migrations",
    "pending_migrations",
    "status",
]
