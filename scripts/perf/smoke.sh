#!/usr/bin/env bash
# 压测前置冒烟（PR-302）：确认目标站的端点真的存在、/ready 当前状态、证书到期日。
# 目的不是压流量，而是**别把环境问题测成性能问题**。
#
# 用法：BASE_URL=https://quant.example.com scripts/perf/smoke.sh
#       BASE_URL=http://127.0.0.1:8899 INSECURE=1 scripts/perf/smoke.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8899}"
BASE_URL="${BASE_URL%/}"
INSECURE="${INSECURE:-0}"
AUTH_TOKEN="${AUTH_TOKEN:-}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

command -v curl >/dev/null 2>&1 || die "需要 curl"

CURL=(curl -sS -o /dev/null -w '%{http_code}' --max-time 15)
[[ "$INSECURE" == "1" ]] && CURL+=(-k)
[[ -n "$AUTH_TOKEN" ]] && CURL+=(-H "Authorization: Bearer ${AUTH_TOKEN}")

code() {
  local url="$1"
  local out
  out="$("${CURL[@]}" "$url" 2>/dev/null || true)"
  [[ -n "$out" ]] || out="000"
  printf '%s' "$out"
}

fail=0
check() {
  # $1 路径  $2 期望码（支持 200|429 形式，任一命中即通过）  $3 说明
  local path="$1" expect="$2" desc="$3" c
  c="$(code "${BASE_URL}${path}")"
  local ok=0
  for e in ${expect//|/ }; do
    [[ "$c" == "$e" ]] && ok=1
  done
  if [[ "$ok" == "1" ]]; then
    log "OK   ${path} -> ${c}（${desc}）"
  else
    log "FAIL ${path} -> ${c}，期望 ${expect}（${desc}）"
    fail=1
  fi
  return 0
}

log "目标: ${BASE_URL}（insecure=${INSECURE}）"
check /live 200 "进程存活"
check /metrics 200 "Prometheus 抓取端点"

# /ready 允许 200 或 503：503 是**诚实降级**，不是 bug；但压测前要看清楚原因。
ready_code="$(code "${BASE_URL}/ready")"
if [[ "$ready_code" == "200" ]]; then
  log "OK   /ready -> 200（依赖可用）"
else
  body="$(curl -sS --max-time 15 ${INSECURE:+-k} "${BASE_URL}/ready" 2>/dev/null | head -c 400 || true)"
  log "WARN /ready -> ${ready_code}（降级/不可用）：$(printf '%s' "$body" | tr -d '\n' | cut -c1-200)"
fi

# 证书到期日是 PR-303「证书过期」演练的输入；连不上就如实说连不上，不猜一个日期。
if [[ "$BASE_URL" == https://* ]] && command -v openssl >/dev/null 2>&1; then
  host="${BASE_URL#https://}"
  host="${host%%/*}"
  expiry="$(echo | openssl s_client -connect "${host}:443" -servername "$host" 2>/dev/null \
    | openssl x509 -noout -enddate 2>/dev/null || true)"
  if [[ -n "$expiry" ]]; then
    log "TLS 证书到期：${expiry#notAfter=}"
  else
    log "WARN 未能读取 TLS 证书到期时间（openssl 握手失败？）"
  fi
fi

if [[ "$fail" != "0" ]]; then
  die "冒烟未通过：先修环境，再压测（否则测的是故障不是容量）"
fi
log "冒烟通过。下一步：k6 run scripts/perf/loadtest.js"
