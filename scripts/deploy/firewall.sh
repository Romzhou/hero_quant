#!/usr/bin/env bash
# 防火墙（PR-162）：只开 SSH + 80/443（tcp）+ 443/udp（HTTP/3）。
# 不做 reset，避免清掉既有运维规则；幂等追加。
#
# 用法：firewall.sh --ssh-port 22 [--dry-run]
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SSH_PORT=""
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ssh-port) SSH_PORT="${2:?}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) die "未知参数: $1" ;;
  esac
done

[[ -n "$SSH_PORT" ]] || die "必须显式给 --ssh-port（默认 22 也可能被改过，猜错会锁死 SSH）"
[[ "$SSH_PORT" =~ ^[0-9]+$ ]] && (( SSH_PORT > 0 && SSH_PORT < 65536 )) || die "非法端口: $SSH_PORT"
require_cmd ufw
[[ "$(id -u)" == "0" ]] || die "需要 root"

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf 'DRY-RUN: %s\n' "$*"
  else
    "$@"
  fi
}

log "ufw: 默认拒绝入站、放行出站，仅开 ${SSH_PORT}/tcp + 80/443"
run ufw default deny incoming
run ufw default allow outgoing
run ufw allow "${SSH_PORT}/tcp" comment "ssh"
run ufw allow 80/tcp comment "http-acme"
run ufw allow 443/tcp comment "https"
run ufw allow 443/udp comment "http3"

if [[ "$DRY_RUN" == "1" ]]; then
  log "dry-run 结束（未启用 ufw）"
  exit 0
fi

ufw --force enable
ufw status verbose
log "完成。确认 SSH 仍可登录后再关闭当前会话。"
