#!/usr/bin/env bash
# 蓝绿切换（PR-165）：改 upstream -> caddy reload -> 验证 -> 记录颜色。
# 用法：
#   bluegreen.sh status
#   bluegreen.sh activate blue|green     # 目标色必须已健康运行
#   bluegreen.sh switch                  # 切到另一色（另一色未运行会直接失败）
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

cmd="${1:-status}"

load_env
seed_upstream

reload_caddy() {
  compose exec -T caddy caddy reload --config /etc/caddy-src/Caddyfile
}

render_upstream() {
  local color="$1"
  printf '# managed by scripts/deploy/bluegreen.sh -- do not edit while stack is running\n'
  printf 'reverse_proxy app-%s:8899 {\n\tflush_interval -1\n}\n' "$color"
}

sync_systemd_color() {
  local color="$1"
  [[ "$(id -u)" == "0" ]] || return 0
  command -v systemctl >/dev/null 2>&1 || return 0
  local dropin="/etc/systemd/system/hero-quant.service.d"
  mkdir -p "$dropin"
  printf '[Service]\nEnvironment=COMPOSE_PROFILES=%s\n' "$color" >"${dropin}/10-color.conf"
  systemctl daemon-reload >/dev/null 2>&1 || log "WARN systemctl daemon-reload 失败，开机颜色可能仍为旧值"
}

activate() {
  local target="$1"
  [[ "$target" == "blue" || "$target" == "green" ]] || die "非法颜色: $target"
  local current upstream="${HERO_CADDY_STATE_DIR}/upstream.caddy"

  # 目标色必须先在跑且 /ready 通过，避免把流量切到半启动实例。
  if ! compose ps -q "app-${target}" | grep -q .; then
    die "app-${target} 未运行，拒绝切换（先 deploy.sh --color ${target}）"
  fi
  "${REPO_ROOT}/scripts/deploy/healthcheck.sh" --service "app-${target}" --retries 5 --interval 2 >/dev/null \
    || die "app-${target} /ready 未通过，拒绝切换"

  current="$(active_color)"
  if [[ "$current" == "$target" ]]; then
    log "已经是 ${target} 色，无需切换"
    set_active_color "$target"
    return 0
  fi

  mkdir -p "$STATE_DIR"
  cp "$upstream" "${STATE_DIR}/upstream.prev"  render_upstream "$target" >"${upstream}.tmp"
  mv "${upstream}.tmp" "$upstream"

  if ! reload_caddy; then
    log "ERROR: caddy reload 失败，回滚 upstream 到 ${current}"
    cp "${STATE_DIR}/upstream.prev" "$upstream"
    reload_caddy || log "ERROR: 回滚 reload 也失败，边缘可能中断，立即人工介入"
    exit 1
  fi

  set_active_color "$target"
  sync_systemd_color "$target"
  log "已切换流量: ${current} -> ${target}（app-${target}:8899）"
}

case "$cmd" in
  status)
    log "active_color = $(active_color)"
    log "upstream.caddy:"
    sed 's/^/    /' "${HERO_CADDY_STATE_DIR}/upstream.caddy"
    compose ps app-blue app-green caddy || true
    ;;
  activate)
    [[ $# -ge 2 ]] || die "用法: bluegreen.sh activate blue|green"
    activate "$2"
    ;;
  switch)
    activate "$(other_color "$(active_color)")"
    ;;
  *)
    die "用法: bluegreen.sh status|activate <blue|green>|switch"
    ;;
esac
