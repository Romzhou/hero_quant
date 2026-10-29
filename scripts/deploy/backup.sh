#!/usr/bin/env bash
# 每日备份（PR-164）：PG 逻辑全备 + 物理 base backup（PITR 基线）+ Redis RDB + 清单。
# WAL 由 postgres 的 archive_command 实时写到 ${HERO_BACKUP_DIR}/wal（bind mount），
# 本脚本只做保留期清理；WAL 保留天数必须 >= 全备保留天数，否则 PITR 会断档。
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

load_env
require_cmd docker sha256sum find awk

KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
WAL_KEEP_DAYS="${WAL_KEEP_DAYS:-30}"
[[ "$KEEP_DAYS" =~ ^[0-9]+$ && "$WAL_KEEP_DAYS" =~ ^[0-9]+$ ]] || die "保留天数必须是整数"
(( WAL_KEEP_DAYS >= KEEP_DAYS )) || die "WAL_KEEP_DAYS(${WAL_KEEP_DAYS}) 必须 >= BACKUP_KEEP_DAYS(${KEEP_DAYS})"

compose ps -q postgres | grep -q . || die "postgres 未运行，无法备份"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${HERO_BACKUP_DIR}/daily/${TS}"
mkdir -p "$DEST"
chmod 700 "$DEST"

log "备份开始: ${DEST}"

# 1) 逻辑全备：可跨小版本恢复，是 PITR 失败时的兜底。
compose exec -T -u postgres postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' >"${DEST}/heroquant.dump"
# 2) 角色/权限（恢复时需要先有角色）
compose exec -T -u postgres postgres sh -c \
  'pg_dumpall -U "$POSTGRES_USER" --globals-only' >"${DEST}/globals.sql"
# 3) 物理基线：-Ft -z -X stream 自包含，供 PITR 恢复使用。
compose exec -T -u postgres postgres sh -c \
  'pg_basebackup -U "$POSTGRES_USER" -D - -Ft -z -X stream -c fast' >"${DEST}/pgbase.tar.gz"

# 4) Redis：缓存/限流可重算，备份失败只告警不阻断（PG 是真相源）。
if compose ps -q redis | grep -q .; then
  compose exec -T redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --no-auth-warning BGSAVE' >/dev/null
  for _ in $(seq 1 30); do
    state="$(compose exec -T redis sh -c \
      'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --no-auth-warning info persistence' \
      | tr -d '\r' | awk -F: '/^rdb_bgsave_in_progress:/{print $2}')"
    [[ "$state" == "0" ]] && break
    sleep 2
  done
  if compose cp redis:/data/dump.rdb "${DEST}/redis.rdb" >/dev/null 2>&1; then
    log "Redis RDB 已备份"
  else
    log "WARN Redis RDB 备份失败（缓存可重算，不阻断）"
  fi
fi

{
  echo "timestamp=${TS}"
  echo "image_tag=$(tr -d '[:space:]' <"${STATE_DIR}/active_tag" 2>/dev/null || echo unknown)"
  echo "host=$(hostname)"
  echo "wal_archived_files=$(find "${HERO_BACKUP_DIR}/wal" -maxdepth 1 -type f 2>/dev/null | wc -l)"
  echo "keep_days=${KEEP_DAYS}"
  echo "wal_keep_days=${WAL_KEEP_DAYS}"
} >"${DEST}/MANIFEST"

(
  cd "$DEST"
  sha256sum -- ./* >SHA256SUMS
)

# 5) 保留期：先清全备，再清 WAL（WAL 天数 >= 全备天数由上面的校验保证）。
find "${HERO_BACKUP_DIR}/daily" -mindepth 1 -maxdepth 1 -type d -mtime "+${KEEP_DAYS}" -exec rm -rf {} +
find "${HERO_BACKUP_DIR}/wal" -maxdepth 1 -type f -mtime "+${WAL_KEEP_DAYS}" -delete

log "备份完成: $(du -sh "$DEST" | awk '{print $1}')，WAL 保留 ${WAL_KEEP_DAYS} 天，全备保留 ${KEEP_DAYS} 天"
