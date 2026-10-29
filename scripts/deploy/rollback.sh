#!/usr/bin/env bash
# 回滚到上一个成功发布的镜像（PR-163）。只回滚应用与流量，不回滚数据库 DDL
# （迁移必须向后兼容；跨 DDL 的回滚属人工决策，见 deploy/README.md）。
#
# 用法：rollback.sh [--dry-run]
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *) die "未知参数: $arg" ;;
  esac
done

load_env
[[ -f "${STATE_DIR}/previous_tag" ]] || die "没有 ${STATE_DIR}/previous_tag，无法确定上一版本"
prev_tag="$(tr -d '[:space:]' <"${STATE_DIR}/previous_tag")"
cur_tag="$(tr -d '[:space:]' <"${STATE_DIR}/active_tag" 2>/dev/null || true)"
[[ -n "$prev_tag" ]] || die "previous_tag 为空"

log "回滚计划: ${cur_tag:-<unknown>} -> ${prev_tag}（部署到另一色，迁移跳过）"
if [[ "$DRY_RUN" == "1" ]]; then
  log "--dry-run：不执行"
  exit 0
fi

export HERO_IMAGE_TAG="$prev_tag"
"${REPO_ROOT}/scripts/deploy/deploy.sh" --tag "$prev_tag" --skip-migrate
log "回滚完成: ${prev_tag} 接流量（schema 未回滚；如跨 DDL 请人工评估）"
