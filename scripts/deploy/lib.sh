#!/usr/bin/env bash
# 共享函数库：只被 source，不直接执行。
# 约定：所有脚本以 root 运行（docker 组亦可），env 文件由管理员维护、chmod 600。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/deploy/compose/docker-compose.prod.yml"
HERO_ENV_FILE="${HERO_ENV_FILE:-${REPO_ROOT}/.env.prod}"
STATE_DIR="${HERO_DEPLOY_STATE_DIR:-/var/lib/hero-quant/deploy}"
# 蓝绿切换点放仓库外：git checkout 新版本时不得覆盖正在生效的颜色。
HERO_CADDY_STATE_DIR="${HERO_CADDY_STATE_DIR:-/var/lib/hero-quant/caddy}"

export REPO_ROOT COMPOSE_FILE HERO_ENV_FILE STATE_DIR HERO_CADDY_STATE_DIR

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

require_cmd() {
  local c
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "缺少命令: $c"
  done
}

load_env() {
  local target="$HERO_ENV_FILE"
  [[ -f "$target" ]] || die "env 文件不存在: $target（从 deploy/env/prod.env.example 复制并填值）"
  set -a
  # shellcheck disable=SC1090
  . "$target"
  set +a
  # env 文件里若也写了 HERO_ENV_FILE，会让 compose 去找另一个文件；以命令行/安装路径为准。
  if [[ "$HERO_ENV_FILE" != "$target" ]]; then
    log "WARN env 文件覆盖了 HERO_ENV_FILE=${HERO_ENV_FILE}，忽略，沿用 ${target}"
    HERO_ENV_FILE="$target"
  fi
}

compose() {
  docker compose --env-file "$HERO_ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

active_color() {
  local c="blue"
  if [[ -f "${STATE_DIR}/active_color" ]]; then
    c="$(tr -d '[:space:]' <"${STATE_DIR}/active_color")"
  fi
  [[ "$c" == "blue" || "$c" == "green" ]] || die "active_color 非法: '$c'（只允许 blue/green）"
  printf '%s' "$c"
}

other_color() {
  [[ "$1" == "blue" ]] && printf 'green' || printf 'blue'
}

set_active_color() {
  mkdir -p "$STATE_DIR"
  printf '%s\n' "$1" >"${STATE_DIR}/active_color.tmp"
  mv "${STATE_DIR}/active_color.tmp" "${STATE_DIR}/active_color"
}

# 首次部署把仓库里的模板种子到状态目录；之后只读状态目录（可被 git 覆盖的仓库文件不再参与）。
seed_upstream() {
  mkdir -p "$HERO_CADDY_STATE_DIR"
  if [[ ! -f "${HERO_CADDY_STATE_DIR}/upstream.caddy" ]]; then
    cp "${REPO_ROOT}/deploy/caddy/upstream.caddy" "${HERO_CADDY_STATE_DIR}/upstream.caddy"
  fi
  chmod 644 "${HERO_CADDY_STATE_DIR}/upstream.caddy"
}

# 部署记录：W8 审计 API 合入前，先落本地 JSONL（append-only，600）。
record_deploy() {
  local result="$1" tag="$2" color="$3" previous="$4" migrated="$5"
  mkdir -p "$STATE_DIR"
  local line
  line="$(printf '{"ts":"%s","result":"%s","tag":"%s","color":"%s","previous_tag":"%s","migrated":%s,"operator":"%s"}' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$result" "$tag" "$color" "$previous" "$migrated" "${SUDO_USER:-${USER:-unknown}}")"
  printf '%s\n' "$line" >>"${STATE_DIR}/deploys.jsonl"
  chmod 600 "${STATE_DIR}/deploys.jsonl"
}

# 只允许字母数字与 . _ - / : @，避免把未转义字符拼进 JSON/日志。
require_safe_tag() {
  [[ "$1" =~ ^[A-Za-z0-9._:@/-]+$ ]] || die "镜像 tag 含非法字符: $1"
}
