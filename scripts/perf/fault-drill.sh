#!/usr/bin/env bash
# 故障演练（PR-303）：注入 -> 观测 -> 恢复 -> 记录。
#
# 用法：
#   scripts/perf/fault-drill.sh --list
#   scripts/perf/fault-drill.sh --case redis-down --confirm
#   scripts/perf/fault-drill.sh --case pg-restart --confirm --observe-seconds 120
#   scripts/perf/fault-drill.sh --case cert-expiry          # 只读，无需 confirm
#
# 安全约束：
#   - 任何破坏性 case 都要 --confirm；
#   - HERO_ENV=production 下还要额外 --allow-disruptive-in-prod（真演练请走维护窗口）；
#   - 脚本退出前一定会尝试恢复（trap），即使中途报错；
#   - 记录落 ${HERO_DRILL_LOG:-<state>/drills.jsonl}。
#
# 诚实边界：
#   - `llm-timeout` / `llm-429` 需要控制 provider 或真实限流，本机注入不了，
#     所以 **不提供这两个 case**，不假装演练过（见 --list）。
#   - `disk-full` 会填盘，在共享机上风险大于收益，只提供只读的 `disk-report`。
set -euo pipefail
# shellcheck source=../deploy/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/../deploy/lib.sh"

CASE=""
CONFIRM=0
ALLOW_PROD=0
OBSERVE_SECONDS="${OBSERVE_SECONDS:-90}"
RECOVERY_TIMEOUT="${RECOVERY_TIMEOUT:-300}"
DRILL_LOG="${HERO_DRILL_LOG:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --case) CASE="${2:?}"; shift 2 ;;
    --confirm) CONFIRM=1; shift ;;
    --allow-disruptive-in-prod) ALLOW_PROD=1; shift ;;
    --observe-seconds) OBSERVE_SECONDS="${2:?}"; shift 2 ;;
    --recovery-timeout) RECOVERY_TIMEOUT="${2:?}"; shift 2 ;;
    --list) CASE="__list__"; shift ;;
    *) die "未知参数: $1（先用 --list）" ;;
  esac
done

DESTRUCTIVE="redis-down pg-restart app-kill"
READONLY="cert-expiry disk-report"

list_cases() {
  cat <<'EOF'
可用 case：
  redis-down   停 redis 容器；期望 /ready 变 503（reasons 含 redis），恢复后回到 200
  pg-restart   重启 postgres；期望 /ready 短暂降级，随后恢复
  app-kill     kill 当前颜色的应用容器；期望 compose 自愈（restart: unless-stopped）
  cert-expiry  只读：读取 TLS 证书到期日（用于"证书过期"演练台账）
  disk-report  只读：备份盘剩余空间 + PG 归档状态

未实现（不假装有）：
  llm-timeout / llm-429 —— 需要控制 provider 或真实限流，本机无法注入
  disk-full             —— 填盘在共享机上风险过大，停机 drill 请在隔离机上手工做
EOF
}

if [[ "$CASE" == "__list__" ]]; then
  list_cases
  exit 0
fi
[[ -n "$CASE" ]] || die "必须给 --case（--list 看清单）"
is_destructive=0
for c in $DESTRUCTIVE; do [[ "$CASE" == "$c" ]] && is_destructive=1; done
is_readonly=0
for c in $READONLY; do [[ "$CASE" == "$c" ]] && is_readonly=1; done
[[ "$is_destructive" == "1" || "$is_readonly" == "1" ]] || die "未知 case: ${CASE}（--list）"

load_env
color="$(active_color)"
[[ -z "$DRILL_LOG" ]] && DRILL_LOG="${STATE_DIR}/drills.jsonl"

if [[ "$is_destructive" == "1" ]]; then
  [[ "$CONFIRM" == "1" ]] || die "破坏性演练必须加 --confirm"
  if [[ "${HERO_ENV:-}" == "production" && "$ALLOW_PROD" != "1" ]]; then
    die "生产环境演练需额外 --allow-disruptive-in-prod（请放到维护窗口）"
  fi
fi

# --- 观测手段：优先容器内探针；容器停了拿不到就用 000 表示"探针也失败" --------
ready_code() {
  local out
  out="$(compose exec -T "app-${color}" python -c \
    "import urllib.request,urllib.error
try:
    r=urllib.request.urlopen('http://127.0.0.1:8899/ready', timeout=4); print(r.status)
except urllib.error.HTTPError as e: print(e.code)
except Exception: print(0)" 2>/dev/null || true)"
  [[ -n "$out" ]] || out="000"
  printf '%s' "$(printf '%s' "$out" | tr -d '\r' | tail -n1)"
}

now_ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

record() {
  # $1 result  $2 detected_s  $3 recovered_s  $4 notes
  mkdir -p "$(dirname "$DRILL_LOG")"
  printf '{"ts":"%s","case":"%s","env":"%s","color":"%s","result":"%s","detected_s":%s,"recovered_s":%s,"notes":"%s"}\n' \
    "$(now_ts)" "$CASE" "${HERO_ENV:-unknown}" "$color" "$1" "${2:-0}" "${3:-0}" "$4" >>"$DRILL_LOG"
  chmod 600 "$DRILL_LOG" 2>/dev/null || true
}

restore_all() {
  local rc=$?
  [[ "$RESTORE_DONE" == "1" ]] && return 0
  RESTORE_DONE=1
  log "恢复中（case=${CASE}）"
  case "$CASE" in
    redis-down) compose up -d redis >/dev/null 2>&1 || true ;;
    pg-restart) : ;;
    app-kill) compose --profile "$color" up -d --no-deps "app-${color}" >/dev/null 2>&1 || true ;;
  esac
  [[ "$rc" != "0" ]] && log "WARN 演练脚本以 rc=${rc} 退出，已尽力恢复"
  return 0
}
RESTORE_DONE=0

run_readonly() {
  case "$CASE" in
    cert-expiry)
      local domain="${HERO_DOMAIN:?HERO_DOMAIN required}"
      if command -v openssl >/dev/null 2>&1; then
        expiry="$(echo | openssl s_client -connect "${domain}:443" -servername "$domain" 2>/dev/null \
          | openssl x509 -noout -enddate 2>/dev/null || true)"
        if [[ -n "$expiry" ]]; then
          log "${domain} 证书到期：${expiry#notAfter=}"
        else
          log "WARN 读不到证书（${domain}:443 不可达？）"
        fi
      else
        log "WARN 未安装 openssl，跳过证书检查"
      fi
      ;;
    disk-report)
      log "备份盘: $(df -h "$HERO_BACKUP_DIR" | awk 'NR==2 {print $2" total, "$4" avail, "$5" used"}')"
      log "PGDATA/WAL 归档: $(find "${HERO_BACKUP_DIR}/wal" -maxdepth 1 -type f 2>/dev/null | wc -l) 个 WAL 文件"
      compose exec -T -u postgres postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
        "SELECT 'last_failed_wal=' || failed_count FROM pg_stat_archiver" 2>/dev/null \
        || log "WARN pg_stat_archiver 查询失败"
      ;;
  esac
  record "readonly" 0 0 "只读检查，未注入故障"
}

run_drill() {
  trap restore_all EXIT
  local before detected elapsed=0
  before="$(ready_code)"
  log "注入前 /ready=${before}（color=${color}）"

  case "$CASE" in
    redis-down) compose stop redis >/dev/null ;;
    pg-restart) compose restart postgres >/dev/null ;;
    app-kill) compose kill "app-${color}" >/dev/null ;;
  esac
  log "已注入 ${CASE}"

  detected="not-detected"
  while (( elapsed < OBSERVE_SECONDS )); do
    local cur
    cur="$(ready_code)"
    if [[ "$CASE" == "redis-down" && "$cur" != "200" ]]; then
      detected="$elapsed"; break
    fi
    if [[ "$CASE" == "pg-restart" || "$CASE" == "app-kill" ]]; then
      # 这两类期望"先坏后自愈"，观测窗口内只要出现过非 200 就算命中
      [[ "$cur" != "200" ]] && { detected="$elapsed"; break; }
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  log "观测结果: detected_s=${detected}（窗口 ${OBSERVE_SECONDS}s）"

  restore_all

  elapsed=0
  while (( elapsed < RECOVERY_TIMEOUT )); do
    if [[ "$(ready_code)" == "200" ]]; then
      log "恢复完成: ${elapsed}s 后 /ready=200"
      record "success" "${detected}" "$elapsed" "注入 ${CASE}，观测 ${OBSERVE_SECONDS}s"
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done

  log "ERROR: ${RECOVERY_TIMEOUT}s 内 /ready 未回到 200"
  record "failed" "${detected}" "-1" "恢复超时 ${RECOVERY_TIMEOUT}s，需人工介入"
  return 1
}

if [[ "$is_readonly" == "1" ]]; then
  run_readonly
else
  run_drill
fi
