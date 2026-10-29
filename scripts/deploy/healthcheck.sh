#!/usr/bin/env bash
# /ready 探针（PR-163 的门）：容器内探针默认走 python urllib，不依赖宿主 curl。
# 用法：
#   healthcheck.sh --service app-blue [--retries 30] [--interval 2]
#   healthcheck.sh --url https://quant.example.com/ready [--insecure]
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SERVICE=""
URL=""
RETRIES=30
INTERVAL=2
INSECURE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service) SERVICE="${2:?}"; shift 2 ;;
    --url) URL="${2:?}"; shift 2 ;;
    --retries) RETRIES="${2:?}"; shift 2 ;;
    --interval) INTERVAL="${2:?}"; shift 2 ;;
    --insecure) INSECURE=1; shift ;;
    *) die "未知参数: $1" ;;
  esac
done

[[ -n "$SERVICE" || -n "$URL" ]] || die "必须给 --service 或 --url"

probe() {
  if [[ -n "$URL" ]]; then
    local args=(-fsS --max-time 6)
    [[ "$INSECURE" == "1" ]] && args+=(-k)
    curl "${args[@]}" "$URL" >/dev/null
  else
    compose exec -T "$SERVICE" python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8899/ready', timeout=4)" >/dev/null
  fi
}

target="${URL:-service=$SERVICE}"
log "等待 /ready: ${target}（最多 $((RETRIES * INTERVAL))s）"
i=1
while (( i <= RETRIES )); do
  if probe; then
    log "/ready 通过（第 ${i} 次探测）"
    exit 0
  fi
  if (( i % 5 == 0 )); then
    log "仍未就绪（${i}/${RETRIES}），继续等待…"
  fi
  sleep "$INTERVAL"
  i=$((i + 1))
done

log "ERROR: /ready 在 $((RETRIES * INTERVAL))s 内未通过: ${target}"
exit 1
