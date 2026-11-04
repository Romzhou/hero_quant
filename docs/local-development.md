# 本地开发（批次16）

## 1. 跑测试：只有一个入口

```bash
scripts/test_local.sh                # 全量（自动注入 PG DSN）
scripts/test_local.sh -q -k queue    # 透传给 pytest
```

**不要**直接 `pytest -q`：本机/CI 的差异几乎都出在这一步。直接跑时 conftest 会**主动中和**
`HERO_*_DSN` 环境变量（防 .env 把离线用例偷偷接到真库），于是 PG 族 82 条整族 skip，
表现为"本地 90 skipped、CI 8 skipped"——本地绿不代表 CI 绿。

`scripts/test_local.sh` 做的事：复用 `scripts/run_pg_tests.py` 的 DSN 解析（键顺序
`HERO_CHECKPOINT_DSN → HERO_PG_DSN → HERO_PG_HOST/USER/PASSWORD`），把结果以环境变量
显式传给 pytest。解析不出 DSN 就 exit 2 并提示缺哪个键，**不会**静默 skip。

实测（2026-11-01，本机）：
| 跑法 | 结果 |
|---|---|
| `pytest -q`（无 DSN） | 3581 passed / **90 skipped** |
| `scripts/test_local.sh -q` | **3668 passed / 3 skipped** |
| CI（`test.yml`） | 3663 passed / 8 skipped |

三者总数都是 3671 条；差异全在"环境门控"的那几条，没有测试丢失。

## 2. 本机 PostgreSQL

```bash
docker ps --format "table {{.Names}}	{{.Image}}	{{.Ports}}" | grep -i postgres
# llm-pg   postgres:16-alpine   100.114.54.26:5432->5432/tcp
```

- 本机：容器 `llm-pg`，镜像 **`postgres:16-alpine`**，库名 `heroquant`。
- CI：`image: postgres:16`（Debian 变体），库名 `hero_quant`，账号 `postgres/postgres`。

**同一 major（16）**，差异只有两点：基础镜像变体（alpine/musl vs Debian/glibc）与库名。
PG 集成用例每个用例建独立临时 schema 并在结束时 `DROP CASCADE`，所以库名差异不影响用例。
若某天出现 collation/排序相关失败，先把 CI 镜像改成与本地一致的 `postgres:16-alpine` 再判断。

## 3. PG 测试怎么隔离

- 每个文件用 `pytestmark = skipif(pg_guard_reason())`：探测 DSN → TCP 0.3s → `psycopg.connect(2s)`，
  任一步失败就整族 skip，reason 里带"为什么不可用"（不回显口令）。
- 用例内建临时 schema（`apply_migrations` + `DROP SCHEMA ... CASCADE`），不污染共享库。
- `pg_test_dsn()` 的 DSN 首次解析后**冻结**，避免后续用例改 env 造成"守卫连 A、用例连 B"。

## 4. 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 本地 skipped = 90 | 直接跑了 `pytest`，没有 DSN | 用 `scripts/test_local.sh` |
| `fe_sendauth: no password supplied` | DSN 有 user 没口令 | 在 `.env` 配 `HERO_PG_PASSWORD`（走 provider 注入） |
| `test_local: 解析不出 DSN` | `.env` 缺键或 docker 没起 | 先 `docker ps` 确认 `llm-pg` 在跑 |
| PG 族整族 skip 但库是通的 | 显式配了非 PG scheme 的 DSN | 守卫会报 `PgDsnConfigError`，按提示修正 scheme |
