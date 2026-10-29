#!/usr/bin/env bash
# 恢复（PR-164）。两种模式，都会先停应用，属于破坏性操作，必须显式 --yes-destroy。
#
#   restore.sh logical --dump <heroquant.dump> --yes-destroy
#       恢复到当前数据库（--clean --if-exists），适合误删/坏数据回退。
#   restore.sh pitr --base <pgbase.tar.gz> --target-time '2026-10-29 03:00:00+00' --yes-destroy
#       用物理基线与 WAL 归档恢复到指定时间点；旧 PGDATA 保留为 data.old.<epoch>。
#
# 演练要求：先在 staging 跑一遍并记录 RPO/RTO；未演练前不得对外承诺恢复时间。
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

MODE="${1:-}"
shift || true
DUMP=""
BASE=""
TARGET_TIME=""
CONFIRM=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dump) DUMP="${2:?}"; shift 2 ;;
    --base) BASE="${2:?}"; shift 2 ;;
    --target-time) TARGET_TIME="${2:?}"; shift 2 ;;
    --yes-destroy) CONFIRM=1; shift ;;
    *) die "未知参数: $1" ;;
  esac
done

[[ "$CONFIRM" == "1" ]] || die "破坏性操作：确认后加 --yes-destroy"
load_env

psql_db() {
  compose exec -T -u postgres postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"
}

stop_apps() {
  compose stop app-blue app-green >/dev/null 2>&1 || true
  log "应用已停（blue/green）"
}

start_apps() {
  local color; color="$(active_color)"
  compose --profile "$color" up -d --no-deps "app-${color}"
  "${REPO_ROOT}/scripts/deploy/healthcheck.sh" --service "app-${color}" --retries 60 --interval 2
}

restore_logical() {
  [[ -f "$DUMP" ]] || die "dump 不存在: $DUMP"
  compose ps -q postgres | grep -q . || die "postgres 未运行"
  stop_apps
  log "终止 ${POSTGRES_DB} 现有连接"
  compose exec -T -u postgres postgres psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -c \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${POSTGRES_DB}' AND pid <> pg_backend_pid()" >/dev/null
  log "pg_restore --clean --if-exists（单事务）"
  compose exec -T -u postgres postgres pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    --clean --if-exists --no-owner --single-transaction --exit-on-error <"$DUMP"
  tables="$(psql_db -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")"
  (( tables >= 2 )) || die "恢复后 public 表数量异常: ${tables}"
  log "恢复完成，public 表 ${tables} 张"
  start_apps
}

restore_pitr() {
  [[ -f "$BASE" ]] || die "base 不存在: $BASE"
  [[ "$TARGET_TIME" =~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9:.+ -]+$' ]] || die "target-time 格式非法"
  [[ -d "${HERO_BACKUP_DIR}/wal" ]] || die "WAL 归档目录不存在: ${HERO_BACKUP_DIR}/wal"
  stop_apps
  compose stop postgres >/dev/null
  if compose ps -q postgres | grep -q .; then
    die "postgres 未停止，拒绝动 PGDATA"
  fi

  log "解包物理基线到 PGDATA（旧目录保留为 data.old.<epoch>）"
  compose run --rm --no-deps -T -e TARGET_TIME="$TARGET_TIME" --entrypoint sh postgres -c '
    set -e
    PGDATA=/var/lib/postgresql/data
    if [ -d "$PGDATA" ] && [ -n "$(ls -A "$PGDATA" 2>/dev/null)" ]; then
      mv "$PGDATA" "${PGDATA}.old.$(date +%s)"
    fi
    mkdir -p "$PGDATA"
    tar -xzf - -C "$PGDATA"
    touch "$PGDATA/recovery.signal"
    {
      echo "restore_command = '\''cp /var/lib/postgresql/wal-archive/%f %p'\''"
      echo "recovery_target_time = '\''${TARGET_TIME}'\''"
      echo "recovery_target_action = '\''promote'\''"
    } >> "$PGDATA/postgresql.auto.conf"
    chown -R 70:70 "$PGDATA"
  ' <"$BASE"

  log "启动 postgres 并回放 WAL（目标 ${TARGET_TIME}）"
  compose up -d postgres
  local i
  for i in $(seq 1 450); do
    if compose exec -T -u postgres postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
      if [[ "$(psql_db -tAc 'SELECT pg_is_in_recovery()' | tr -d '[:space:]')" == "f" ]]; then
        log "PITR 完成，已 promote"
        start_apps
        return 0
      fi
    fi
    sleep 2
  done

  die "PITR 超时（15 分钟）未完成。检查 ${HERO_BACKUP_DIR}/wal 是否缺 WAL；旧数据在 data.old.* 下。"
}

case "$MODE" in
  logical)
    restore_logical
    ;;
  pitr)
    restore_pitr
    ;;
  *)
    die "用法: restore.sh logical --dump <file> --yes-destroy | restore.sh pitr --base <file> --target-time <ts> --yes-destroy"
    ;;
esac
