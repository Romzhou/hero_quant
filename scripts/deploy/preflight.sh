#!/usr/bin/env bash
# 生产部署前置检查（PR-161/162/164）。默认只读；--init-dirs 才创建备份目录。
# 用法：preflight.sh [--init-dirs] [--quiet]
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

INIT_DIRS=0
QUIET=0
for arg in "$@"; do
  case "$arg" in
    --init-dirs) INIT_DIRS=1 ;;
    --quiet) QUIET=1 ;;
    *) die "未知参数: $arg" ;;
  esac
done

say() { [[ "$QUIET" == "1" ]] || log "$*"; }
fail=0

require_cmd docker
docker compose version >/dev/null 2>&1 || die "需要 docker compose v2"
compose_ver="$(docker compose version --short 2>/dev/null | sed 's/^v//')"
if [[ "$(printf '%s\n2.24\n' "$compose_ver" | sort -V | head -n1)" != "2.24" ]]; then
  die "docker compose 版本过低: ${compose_ver}（env_file.required 需要 >= 2.24）"
fi
say "OK   docker compose ${compose_ver}"

load_env

for v in HERO_IMAGE HERO_IMAGE_TAG HERO_DOMAIN ACME_EMAIL HERO_DB_DSN HERO_REDIS_DSN \
         POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB REDIS_PASSWORD HERO_BACKUP_DIR \
         HERO_CADDY_STATE_DIR COMPOSE_PROFILES; do
  [[ -n "${!v:-}" ]] || { log "FAIL 环境变量为空: $v"; fail=1; }
done
[[ "$fail" == "0" ]] && say "OK   env 必填项齐全"

perms="$(stat -c '%a' "$HERO_ENV_FILE")"
if (( (8#$perms & 8#077) != 0 )); then
  log "FAIL env 文件权限 ${perms}，要求 600（含密钥）"; fail=1
else
  say "OK   env 文件权限 ${perms}"
fi

# 密码会被三种解析器读取（shell source / systemd EnvironmentFile / compose），
# 只允许字母数字与 . _ -，避免引号与 $ 造成静默截断。
for v in POSTGRES_PASSWORD REDIS_PASSWORD; do
  val="${!v:-}"
  if [[ -n "$val" && ! "$val" =~ ^[A-Za-z0-9._-]+$ ]]; then
    log "FAIL $v 含不安全字符（仅允许 A-Za-z0-9._-，建议 32 位随机字母数字）"; fail=1
  fi
done

if [[ "$INIT_DIRS" == "1" ]]; then
  mkdir -p "${HERO_BACKUP_DIR}/daily" "${HERO_BACKUP_DIR}/wal"
  chmod 750 "$HERO_BACKUP_DIR" "${HERO_BACKUP_DIR}/daily" "${HERO_BACKUP_DIR}/wal"
  # postgres:16-alpine 容器内 uid/gid = 70；WAL archive_command 以该身份写入。
  chown 70:70 "${HERO_BACKUP_DIR}/wal" 2>/dev/null \
    || die "需要 root 才能把 ${HERO_BACKUP_DIR}/wal 交给 uid 70（postgres 容器身份）"
  seed_upstream
  say "OK   已初始化备份目录与蓝绿状态目录"
fi

if [[ -d "$HERO_BACKUP_DIR/wal" && -w "$HERO_BACKUP_DIR/wal" ]]; then
  owner="$(stat -c '%u:%g' "$HERO_BACKUP_DIR/wal")"
  if [[ "$owner" != "70:70" ]]; then
    log "FAIL ${HERO_BACKUP_DIR}/wal 属主为 ${owner}，要求 70:70（postgres 容器 uid）"; fail=1
  else
    say "OK   备份目录属主/可写"
  fi
else
  log "FAIL 备份目录不存在或不可写: $HERO_BACKUP_DIR/wal（用 --init-dirs）"; fail=1
fi

if [[ -f "${HERO_CADDY_STATE_DIR}/upstream.caddy" ]]; then
  say "OK   蓝绿上游文件存在: ${HERO_CADDY_STATE_DIR}/upstream.caddy"
else
  log "FAIL 缺少 ${HERO_CADDY_STATE_DIR}/upstream.caddy（用 --init-dirs）"; fail=1
fi

free_gb="$(df -Pk "$HERO_BACKUP_DIR" 2>/dev/null | awk 'NR==2 {printf "%d", $4/1024/1024}')"
if [[ -n "$free_gb" ]] && (( free_gb >= 20 )); then
  say "OK   备份盘剩余 ${free_gb}GB（>=20GB）"
else
  log "FAIL 备份盘剩余 ${free_gb:-?}GB，要求 >=20GB"; fail=1
fi

# 端口检查只在 caddy 未运行时做（重复部署时 80/443 必然被占用）。
if compose ps -q caddy 2>/dev/null | grep -q .; then
  say "OK   caddy 已在运行，跳过 80/443 占用检查"
elif command -v ss >/dev/null 2>&1; then
  busy="$(ss -H -ltn 2>/dev/null | awk '$4 ~ /:(80|443)$/ {print $4}' | head -n 3)"
  if [[ -n "$busy" ]]; then
    log "FAIL 80/443 已被占用: ${busy}"; fail=1
  else
    say "OK   80/443 空闲"
  fi
else
  say "WARN 无 ss 命令，跳过端口检查"
fi

# 发布门：生产必须开着身份验证。HERO_AUTH_MODE 缺省会收敛 enforce
# （settings.py:331-339），模板为了"接线前也能起"预置 off，所以这里显式卡死：
# 生产环境要上线，必须改 enforce；临时放行需显式 HERO_ALLOW_AUTH_OFF=1 并承担风险。
# SLO 硬前提（PR-144）：错误预算规则用 30 天窗口，保留期不足时给出的数不可信。
# 这里只告警不阻断：小盘机器确实可能扛不住 35d，但必须让人看见这个代价。
retention="${HERO_PROM_RETENTION:-35d}"
# 单位换算交给 python（h/d/w/m），值通过 argv 传入，不做字符串拼接。
ret_days="$(python3 - "$retention" <<'PY'
import re
import sys

m = re.fullmatch(r"(\d+)([hdwm]?)", sys.argv[1].strip())
if not m:
    print("?")
else:
    n, unit = int(m.group(1)), (m.group(2) or "h")
    print(round(n * {"h": 1 / 24, "d": 1, "w": 7, "m": 30}[unit], 2))
PY
)" 2>/dev/null || ret_days="?"
if [[ "$ret_days" != "?" ]] && awk -v d="$ret_days" 'BEGIN { exit !(d >= 30) }'; then
  say "OK   Prometheus 保留期 ${retention}（>=30d，SLO 30 天窗口可用）"
else
  log "WARN Prometheus 保留期 ${ret_days:-?}d（${retention}） < 30d：slo.yml 的错误预算规则会失真或为空"
  log "     要评估月度 SLO 就提到 >=35d；明确不评估就忽略这条（staging/dev 属此类）"
fi

if [[ "${HERO_ENV:-}" == "production" ]]; then
  auth_off_bypass="${HERO_ALLOW_AUTH_OFF:-0}"
  if [[ "${HERO_AUTH_MODE:-enforce}" != "enforce" && "$auth_off_bypass" != "1" ]]; then
    log "FAIL 生产环境 HERO_AUTH_MODE=${HERO_AUTH_MODE:-off}，必须为 enforce（G1 发布门）"
    log "     确需临时放行：在 env 里显式写 HERO_ALLOW_AUTH_OFF=1，并在发布记录里写明原因"
    fail=1
  else
    note=""
    [[ "$auth_off_bypass" == "1" ]] && note="（已显式临时放行 HERO_ALLOW_AUTH_OFF=1）"
    say "OK   生产身份门：HERO_AUTH_MODE=${HERO_AUTH_MODE:-enforce}${note}"
  fi
fi

if [[ "$fail" != "0" ]]; then
  die "前置检查未通过（修复后重跑：scripts/deploy/preflight.sh --init-dirs）"
fi
say "preflight 通过"
