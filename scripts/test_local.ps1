# 本地全量测试入口（Windows 版，批次16）。
# 与 scripts/test_local.sh 同逻辑：显式解析 DSN 后再跑 pytest，禁止 conftest 隐式探测。
#
# 用法： powershell -File scripts/test_local.ps1 [-PytestArgs "-q -k queue"]
param([string]$PytestArgs = "")

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$dsn = python -c @"
import importlib.util, pathlib
spec = importlib.util.spec_from_file_location('rpt', pathlib.Path('scripts/run_pg_tests.py'))
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
env = mod._dotenv_values(mod._env_file())
dsn, _ = mod._resolve_dsn(env)
print(dsn or '')
"@

if (-not $dsn) {
    Write-Error "test_local: 解析不出 PostgreSQL DSN；需要的键：HERO_CHECKPOINT_DSN / HERO_PG_DSN，或 HERO_PG_HOST + HERO_PG_USER + HERO_PG_PASSWORD"
    exit 2
}

$env:HERO_CHECKPOINT_DSN = $dsn
$env:HERO_PG_DSN = $dsn
$pytest = if ($PytestArgs) { $PytestArgs.Split(" ") } else { @() }
python -m pytest @pytest
