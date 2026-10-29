"""db.loader — 版本化迁移脚本发现与加载。

约定：
- 迁移文件位于仓库根 `db/migrations/NNNN_name.sql`（0001 起，四位零填充）。
- 文件名决定顺序；版本号必须从 1 连续递增，重复/跳号直接拒绝。
- `Migration.checksum()` 对语句内容做 sha256：已应用迁移被改写时 runner 判为 drift。

旧 `migrations/` 目录（仓库根）是历史冻结区，本模块不读取、不修改。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from hero_quant.db.sql import split_sql_statements

_FILENAME_RE = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")


class MigrationLoadError(RuntimeError):
    """迁移脚本缺失 / 命名非法 / 版本不连续。"""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    source: str = ""

    def checksum(self) -> str:
        """对语句序列做稳定哈希（空白变化也算变更，必须显式评审）。"""
        payload = "\x00".join(self.statements).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def default_migrations_dir() -> Path:
    """解析迁移目录：优先仓库根 `db/migrations`，其次当前工作目录。

    兼容两种运行形态：
    - 源码/容器内（`/app/src/hero_quant/db` → `/app/db/migrations`）
    - 以源码树为 cwd 的脚本（`$PWD/db/migrations`）
    找不到时抛 MigrationLoadError，绝不静默返回空集合（否则「无待应用迁移」是假成功）。
    """
    repo_root = Path(__file__).resolve().parents[3]
    candidates = (repo_root / "db" / "migrations", Path.cwd() / "db" / "migrations")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise MigrationLoadError(
        "db/migrations not found; expected at repo root `db/migrations` "
        f"(tried: {', '.join(str(c) for c in candidates)}). "
        "Production images must COPY the top-level `db/` directory."
    )


def load_migrations(directory: str | Path | None = None) -> tuple[Migration, ...]:
    """加载并校验全部迁移脚本，返回按 version 升序的不可变元组。"""
    base = Path(directory) if directory is not None else default_migrations_dir()
    if not base.is_dir():
        raise MigrationLoadError(f"migrations directory does not exist: {base}")

    files: list[tuple[int, str, Path]] = []
    for path in sorted(base.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if match is None:
            raise MigrationLoadError(
                f"invalid migration filename {path.name!r}; expected NNNN_lower_snake.sql"
            )
        files.append((int(match.group("version")), match.group("name"), path))

    if not files:
        raise MigrationLoadError(f"no migration files in {base}")

    migrations: list[Migration] = []
    seen: set[int] = set()
    for expected, (version, name, path) in enumerate(files, start=1):
        if version in seen:
            raise MigrationLoadError(f"duplicate migration version {version:04d} in {base}")
        if version != expected:
            raise MigrationLoadError(
                f"migration versions must be contiguous from 1: expected {expected:04d}, got {version:04d}"
            )
        seen.add(version)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MigrationLoadError(f"cannot read migration {path}: {exc}") from exc
        statements = tuple(split_sql_statements(raw))
        if not statements:
            raise MigrationLoadError(f"migration {path.name} contains no executable statements")
        migrations.append(Migration(version=version, name=name, statements=statements, source=path.name))

    return tuple(migrations)


__all__ = ["Migration", "MigrationLoadError", "default_migrations_dir", "load_migrations"]
