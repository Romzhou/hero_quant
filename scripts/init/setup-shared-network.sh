#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# setup-shared-network.sh — 把既有 llm-pg / llm-redis 接入 heroquant-shared 网络
#
# 背景：docker-compose.yml 里的 hero-quant 服务用容器主机名 llm-pg / llm-redis
# 连接 PG/Redis，但这俩容器由别的 project 创建，不在本 compose project 的默认
# 网络内。本脚本创建外部网络 heroquant-shared 并把它们 connect 进来（带别名）。
#
# 何时需要重跑（幂等，重复执行安全）：
#   - 首次部署 / 换机器；
#   - llm-pg 或 llm-redis 容器被重建（docker compose down/up、升级镜像、
#     --force-recreate）后 —— 新建容器不会继承旧的网络成员关系，此时
#     `docker network connect` 的别名也随之丢失；
#   - 宿主机重启后 docker 网络仍在，但若容器被重建则同上。
#
# 用法：bash scripts/init/setup-shared-network.sh
# 注意：宿主机直跑 uvicorn 时用 100.114.54.26，容器内才用 llm-pg/llm-redis，
#       两者不可混用（100.114.54.26 只在宿主机 netns 有效）。
# ---------------------------------------------------------------------------
set -euo pipefail

NETWORK="heroquant-shared"
PG_CONTAINER="llm-pg"
REDIS_CONTAINER="llm-redis"

# 幂等护栏：本脚本必须在本机（有 docker CLI）执行。scripts/init/ 同时被
# docker-compose 的 local-stack profile 挂载为 postgres 的 initdb 目录，
# postgres 官方 entrypoint 会执行该目录下可执行的 *.sh —— 容器内没有 docker
# CLI，这里直接静默跳过而不是让 initdb 失败。
if ! command -v docker >/dev/null 2>&1; then
  echo "[skip] docker CLI 不存在，跳过（预期：postgres initdb 目录内执行）" >&2
  exit 0
fi

if docker network inspect "${NETWORK}" >/dev/null 2>&1; then
  echo "[ok] 网络 ${NETWORK} 已存在"
else
  docker network create "${NETWORK}"
  echo "[new] 已创建网络 ${NETWORK}"
fi

connect() {
  local container="$1"
  local alias_name="$2"
  if ! docker inspect "${container}" >/dev/null 2>&1; then
    echo "[fail] 容器 ${container} 不存在：请先启动它，再重跑本脚本" >&2
    return 1
  fi
  # 已连接时 docker 返回 "Error response from daemon: endpoint with name ...
  # already exists in network ..."，视为成功。
  local out
  if out="$(docker network connect --alias "${alias_name}" "${NETWORK}" "${container}" 2>&1)"; then
    echo "[ok] ${container} -> ${NETWORK}（别名 ${alias_name}）"
  elif printf '%s' "${out}" | grep -q "already exists in network"; then
    echo "[ok] ${container} 已在 ${NETWORK} 内（别名 ${alias_name}）"
  else
    printf '%s\n' "${out}" >&2
    return 1
  fi
}

connect "${PG_CONTAINER}" "llm-pg"
connect "${REDIS_CONTAINER}" "llm-redis"

echo "[done] ${NETWORK} 就绪：容器内可用 llm-pg:5432 / llm-redis:6379"
