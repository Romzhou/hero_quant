#!/usr/bin/env bash
# 单机发布（PR-163）：拉镜像 -> 迁移 -> 起新色 -> /ready -> 切流量 -> 停旧色。
# 失败自动把流量切回旧色（旧色在切换成功前不会被停）。
#
# 用法：
#   deploy.sh --tag <immutable-tag> [--color blue|green] [--skip-migrate]
#             [--keep-old] [--skip-preflight]
# 环境：
#   HERO_ENV_FILE        默认 <repo>/.env.prod（生产：/etc/hero-quant/hero-quant.env）
#   HERO_VERIFY_URL      可选，例如 https://quant.example.com/ready；设置后切流后再验一次
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TAG=""
COLOR=""
SKIP_MIGRATE=0
KEEP_OLD=0
SKIP_PREFLIGHT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag) TAG="${2:?}"; shift 2 ;;
    --color) COLOR="${2:?}"; shift 2 ;;
    --skip-migrate) SKIP_MIGRATE=1; shift ;;
    --keep-old) KEEP_OLD=1; shift ;;
    --skip-preflight) SKIP_PREFLIGHT=1; shift ;;
    *) die "未知参数: $1" ;;
  esac
done

load_env
[[ -n "$TAG" ]] || die "缺少 --tag（不可用 latest，必须是不可变 tag）"
require_safe_tag "$TAG"
[[ "$TAG" != "latest" ]] || die "禁止用 latest 发布：无法回滚到确定版本"
export HERO_IMAGE_TAG="$TAG"

if [[ "$SKIP_PREFLIGHT" != "1" ]]; then
  "${REPO_ROOT}/scripts/deploy/preflight.sh" --init-dirs --quiet
fi

previous_tag=""
[[ -f "${STATE_DIR}/active_tag" ]] && previous_tag="$(tr -d '[:space:]' <"${STATE_DIR}/active_tag")"
previous_color="$(active_color)"
if [[ -n "$COLOR" ]]; then
  target_color="$COLOR"
elif [[ ! -f "${STATE_DIR}/active_color" ]]; then
  target_color="blue"  # 首次部署：与 upstream 模板一致
else
  target_color="$(other_color "$previous_color")"
fi
[[ "$target_color" == "blue" || "$target_color" == "green" ]] || die "非法颜色: $target_color"

DEPLOY_DONE=0
on_error() {
  local rc=$?
  [[ "$DEPLOY_DONE" == "1" ]] && exit "$rc"
  log "ERROR: 发布失败（rc=${rc}），把流量切回 ${previous_color}（tag=${previous_tag:-<none>}）"
  if compose ps -q "app-${previous_color}" 2>/dev/null | grep -q .; then
    "${REPO_ROOT}/scripts/deploy/bluegreen.sh" activate "$previous_color" \
      || log "ERROR: 回滚切流失败，立即人工介入"
  else
    log "WARN 旧色 app-${previous_color} 未运行（首次部署？），无可回滚实例"
  fi
  record_deploy "failed" "$TAG" "$target_color" "$previous_tag" "false"
  exit "$rc"
}
trap on_error ERR

log "发布开始: tag=${TAG} ${previous_color} -> ${target_color}（previous_tag=${previous_tag:-<none>}）"

# 0) 数据/边缘服务幂等拉起（不动旧色）
compose --profile "$target_color" up -d postgres redis otel-collector caddy

# 1) 镜像先拉取：迁移容器与新色都用同一个不可变 tag
compose --profile "$target_color" pull "app-${target_color}"

# 2) 迁移在启动新版本之前完成；失败不切流量
migrated="false"
if [[ "$SKIP_MIGRATE" == "1" ]]; then
  log "WARN 跳过迁移（--skip-migrate），schema 版本由操作者自担"
else
  "${REPO_ROOT}/scripts/deploy/migrate.sh" --tag "$TAG" --color "$target_color"
  migrated="true"
fi

# 3) 起新色（--no-deps：数据服务已在第 0 步就绪，不能被重建）
compose --profile "$target_color" up -d --no-deps "app-${target_color}"

# 4) /ready 门：聚合 PG/Redis 探测，503 即失败
"${REPO_ROOT}/scripts/deploy/healthcheck.sh" --service "app-${target_color}" --retries 45 --interval 2

# 5) 切流
"${REPO_ROOT}/scripts/deploy/bluegreen.sh" activate "$target_color"

# 6) 边缘自检（可选）：DNS/TLS 就绪后再验一次，防止 caddy reload 成功但路由错
if [[ -n "${HERO_VERIFY_URL:-}" ]]; then
  "${REPO_ROOT}/scripts/deploy/healthcheck.sh" --url "$HERO_VERIFY_URL" --retries 10 --interval 3
fi

# 7) 停旧色（默认停，保留 --keep-old 供人工快速回滚）
if [[ "$previous_color" != "$target_color" && "$KEEP_OLD" != "1" ]]; then
  if compose ps -q "app-${previous_color}" 2>/dev/null | grep -q .; then
    log "停止旧色 app-${previous_color}"
    compose stop "app-${previous_color}" >/dev/null
  fi
fi

mkdir -p "$STATE_DIR"
if [[ -n "$previous_tag" ]]; then
  printf '%s\n' "$previous_tag" >"${STATE_DIR}/previous_tag"
else
  rm -f "${STATE_DIR}/previous_tag"
fi
printf '%s\n' "$TAG" >"${STATE_DIR}/active_tag"
DEPLOY_DONE=1
record_deploy "success" "$TAG" "$target_color" "$previous_tag" "$migrated"
log "发布完成: ${TAG} 在 ${target_color} 色接流量"
