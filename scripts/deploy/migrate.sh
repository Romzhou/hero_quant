#!/usr/bin/env bash
# 数据库迁移（PR-163：迁移必须是部署前的独立步骤，失败不切流量）。
#
# 与 W4（PR-133 版本化迁移）的契约：
#   1) 首选 `python -m hero_quant.db upgrade`（storage 线已交付，W4/W8 官方入口）；
#   2) 备用 `python -m hero_quant.migrations apply`（若后缀改动）。
# 在 W4 合入前，可用 MIGRATE_ALLOW_LEGACY_BOOTSTRAP=1 走幂等 baseline
# （scripts/init/001_schema.sql），仅用于空白库；W4 合入后必须改回 0。
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

COLOR=""
TAG="${HERO_IMAGE_TAG:-}"
INIT_KC=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --color) COLOR="${2:?}"; shift 2 ;;
    --tag) TAG="${2:?}"; shift 2 ;;
    --init-keycloak-db) INIT_KC=1; shift ;;
    *) die "未知参数: $1" ;;
  esac
done

load_env
[[ -n "$TAG" ]] || die "缺少镜像 tag（--tag 或 HERO_IMAGE_TAG）"
require_safe_tag "$TAG"
export HERO_IMAGE_TAG="$TAG"
COLOR="${COLOR:-$(active_color)}"
[[ "$COLOR" == "blue" || "$COLOR" == "green" ]] || die "非法颜色: $COLOR"

compose ps -q postgres | grep -q . || die "postgres 未运行。先执行: docker compose ... up -d postgres redis"
compose exec -T -u postgres postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null \
  || die "postgres 未就绪（pg_isready 失败）"

if [[ "$INIT_KC" == "1" ]]; then
  if compose exec -T -u postgres postgres psql -U "$POSTGRES_USER" -d postgres -tAc \
      "SELECT 1 FROM pg_database WHERE datname='keycloak'" | grep -q 1; then
    log "keycloak 库已存在，跳过"
  else
    compose exec -T -u postgres postgres psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 \
      -c "CREATE DATABASE keycloak"
    log "已创建 keycloak 库"
  fi
fi

probe_runner() {
  compose --profile "$COLOR" run --rm --no-deps --entrypoint python "app-$COLOR" -c \
    "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('$1') else 1)" >/dev/null 2>&1
}

# 前置（storage 线提醒）：迁移 SQL 随镜像进 /app/db/migrations（Dockerfile `COPY db/ db/`）。
# 若镜像里没有该目录，这里会以 MigrationLoadError 失败——这是**预期拦截**，
# 不要改成"跳过迁移继续发"，否则 schema 与代码版本会静默脱节。
# 另：DSN 用户必须是表 owner（baseline 含 `ALTER TABLE ... FORCE ROW LEVEL SECURITY`，
# 非 owner 执行会失败），用受限角色跑迁移前先把权限给对。

# 1) W4 已合入的入口（storage 线交付）：python -m hero_quant.db upgrade
if probe_runner "hero_quant.db.__main__"; then
  log "执行版本化迁移: python -m hero_quant.db upgrade（tag=${TAG}, color=${COLOR}）"
  compose --profile "$COLOR" run --rm --no-deps --entrypoint python "app-$COLOR" -m hero_quant.db upgrade
  log "迁移完成"
  exit 0
fi

# 2) 备用入口（若 W4 后续改名）：XX apply
if probe_runner "hero_quant.migrations"; then
  log "执行版本化迁移: python -m hero_quant.migrations apply"
  compose --profile "$COLOR" run --rm --no-deps --entrypoint python "app-$COLOR" -m hero_quant.migrations apply
  log "迁移完成"
  exit 0
fi

if [[ "${MIGRATE_ALLOW_LEGACY_BOOTSTRAP:-0}" == "1" ]]; then
  log "W4 迁移入口未就位，按 MIGRATE_ALLOW_LEGACY_BOOTSTRAP=1 执行幂等 baseline"
  compose exec -T -u postgres postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v ON_ERROR_STOP=1 -f - <"${REPO_ROOT}/scripts/init/001_schema.sql" >/dev/null
  log "baseline 完成（scripts/init/001_schema.sql，可重复执行）"
  exit 0
fi

log "ERROR: 镜像内没有版本化迁移入口（等待 W4/PR-133），且未允许 legacy bootstrap。"
log "       要么等 hero_quant.migrations 合入，要么在 env 里显式设置 MIGRATE_ALLOW_LEGACY_BOOTSTRAP=1。"
exit 3
