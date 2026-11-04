#!/usr/bin/env bash
# 本地全量测试唯一入口（批次16）：显式解析 DSN 后再跑 pytest。
#
# 为什么要这个脚本：本机 pytest 不带 DSN 时，PG 族 82 条会整族 skip，于是"本地绿"不代表
# CI 绿。这里复用 scripts/run_pg_tests.py 的解析逻辑（同一套键顺序，不另造一套），
# 把 DSN 以环境变量喂给 pytest —— **不在 conftest 里做隐式探测**，配置错就明确失败。
#
# 用法：
#   scripts/test_local.sh                      # 全量
#   scripts/test_local.sh -q -k queue          # 透传给 pytest
#   HERO_ENV_FILE=/path/to.env scripts/test_local.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# 解释器优先级：显式 PYTHON > 仓库 venv > python3（本机没有裸 "python" 命令）
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x .venv/bin/python ]; then PYTHON=.venv/bin/python; else PYTHON=python3; fi
fi

DSN=$("$PYTHON" - <<'PY'
import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location("rpt", pathlib.Path("scripts/run_pg_tests.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
env = mod._dotenv_values(mod._env_file())
dsn, _source = mod._resolve_dsn(env)
print(dsn or "")
PY
)

if [ -z "$DSN" ]; then
  echo "test_local: 解析不出 PostgreSQL DSN，PG 族会被整族 skip；请先确认 docker 里的 PG 与 .env" >&2
  echo "test_local: 需要的键：HERO_CHECKPOINT_DSN / HERO_PG_DSN，或 HERO_PG_HOST + HERO_PG_USER + HERO_PG_PASSWORD" >&2
  exit 2
fi

# 同时设两个键：conftest 守卫按 HERO_CHECKPOINT_DSN → HERO_PG_DSN 顺序取，且要求显式预置
export HERO_CHECKPOINT_DSN="$DSN"
export HERO_PG_DSN="$DSN"
exec "$PYTHON" -m pytest "$@"
