# 单机生产部署（W7 / PR-161~165）

> 目标环境：一台 8C/16G 起（推荐 16C/32G）、独立数据盘、有公网域名与 80/443 的 Linux。
> 本目录只做**单机**：不做 HA/K8s；需要时按 `docs/production-readiness/phase-3.md` 升级。
> 密钥纪律：真值只写 `/etc/hero-quant/hero-quant.env`（chmod 600），不入库、不贴聊天、不进日志。

## 1. 文件地图

| 文件 | 作用 | 任务 |
|---|---|---|
| `deploy/compose/docker-compose.prod.yml` | 生产编排：caddy + app-blue/green + PG/Redis/OTel（+ 可选 Keycloak） | PR-161 |
| `deploy/caddy/Caddyfile` | TLS、安全头、SSE/WS、/metrics 内网限制、auth 子域 | PR-162 |
| `deploy/caddy/upstream.caddy` | 蓝绿切换点模板（运行时副本在 `/var/lib/hero-quant/caddy/`） | PR-165 |
| `deploy/systemd/hero-quant.service` | 开机自启 / compose 生命周期 | PR-162 |
| `deploy/systemd/hero-quant-backup.{service,timer}` | 每日 03:30 备份 | PR-164 |
| `deploy/monitoring/prometheus.yml` | Prometheus 抓取配置（profile: observability） | PR-302 |
| `deploy/monitoring/alerts.yml` | 告警/记录规则（对应真实指标名） | PR-302 |
| `scripts/perf/loadtest.js` | k6 压测场景（问答/SSE/回测/风控） | PR-302 |
| `scripts/perf/smoke.sh` | 压测前置体检（端点/证书） | PR-302/303 |
| `scripts/perf/fault-drill.sh` | 故障注入→观测→恢复→记录 | PR-303 |
| `deploy/env/{prod,staging,dev}.env.example` | 三环境模板（同机隔离靠 project+端口+目录） | PR-165 |
| `scripts/deploy/preflight.sh` | 前置检查 + 初始化目录 | PR-161/164 |
| `scripts/deploy/deploy.sh` | 拉镜像 → 迁移 → 起新色 → /ready → 切流 → 停旧色 | PR-163 |
| `scripts/deploy/rollback.sh` | 回到上一成功版本（只回应用，不回 DDL） | PR-163 |
| `scripts/deploy/bluegreen.sh` | status / activate / switch | PR-165 |
| `scripts/deploy/migrate.sh` | 迁移独立步骤（W4 契约见文件头） | PR-163 |
| `scripts/deploy/backup.sh` / `restore.sh` | 全备+物理基线 / 逻辑恢复+PITR | PR-164 |
| `scripts/deploy/firewall.sh` | ufw：只开 SSH + 80/443 | PR-162 |
| `.github/workflows/deploy.yml` | tag 发布：构建推送 → 迁移 → 部署 → 失败回滚 | PR-163 |

## 2. 首次部署（从零）

前置：

- 域名 A/AAAA 已指向本机；80/443 未被占用；安全组/防火墙放行。
- Docker Engine + Compose v2.24+；`scripts/deploy/preflight.sh` 会校验。
- 备份盘容量 ≥ 数据量 2 倍；`HERO_BACKUP_DIR` 与 PGDATA 不同盘。
- 一个可 pull GHCR 的 token（CI 用），或本机 `docker login ghcr.io`。

```bash
# 1) 代码与目录
sudo install -d -o "$USER" -g "$USER" /opt/hero-quant
git clone <repo> /opt/hero-quant && cd /opt/hero-quant

# 2) 密钥文件（600；模板里 replace-with-* 全部改掉）
sudo install -d -m 700 /etc/hero-quant
sudo install -m 600 deploy/env/prod.env.example /etc/hero-quant/hero-quant.env
sudoedit /etc/hero-quant/hero-quant.env
export HERO_ENV_FILE=/etc/hero-quant/hero-quant.env

# 3) 防火墙（显式给 SSH 端口，避免锁死自己）
sudo ./scripts/deploy/firewall.sh --ssh-port 22

# 4) 前置检查 + 初始化备份/蓝绿目录（wal 属主必须是 70:70）
sudo -E ./scripts/deploy/preflight.sh --init-dirs

# 5) 拉镜像并首次发布（内含迁移；失败会自动回滚，首次无旧色则直接报错）
docker login ghcr.io
sudo -E ./scripts/deploy/deploy.sh --tag <git-sha 或 vX.Y.Z>

# 6) 验证
sudo -E ./scripts/deploy/healthcheck.sh --service app-blue --retries 45
curl -fsS "https://${HERO_DOMAIN}/ready"
curl -fsS "https://${HERO_DOMAIN}/live"

# 7) 开机自启 + 每日备份
sudo install -m 644 deploy/systemd/hero-quant.service deploy/systemd/hero-quant-backup.service \
  deploy/systemd/hero-quant-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hero-quant.service
sudo systemctl enable --now hero-quant-backup.timer
```

> `HERO_ENV_FILE` 必须是**绝对路径**：compose 的 `env_file` 相对路径以 compose 文件目录为基准。
> `preflight.sh` 会拒绝 `latest`、检查口令字符集（`A-Za-z0-9._-`）与 80/443 占用。
- Keycloak 服务在 `--profile auth` 下，OIDC 联调未完成前保持关闭；
  `migrate.sh --init-keycloak-db` 负责建库。

**G1 发布门**：`HERO_ENV=production` 时 `preflight.sh` 会强制 `HERO_AUTH_MODE=enforce`。
模板里预置的 `off` 只用于"W1 还没接线也能先把栈起来"；要对外使用前必须改 `enforce`
并补齐 `HERO_OIDC_*`。确需临时放行就显式写 `HERO_ALLOW_AUTH_OFF=1`，别用注释糊过去。

## 3. 日常发布

CI（推荐）：

- push tag `v*` 触发 `.github/workflows/deploy.yml`；或手动 `workflow_dispatch` 填 git sha。
- 需要 secrets：`DEPLOY_HOST`、`DEPLOY_USER`、`DEPLOY_SSH_KEY`、`GHCR_PULL_TOKEN`；
  可选 `DEPLOY_PORT`、`DEPLOY_KNOWN_HOSTS`；变量 `DEPLOY_PATH`（默认 `/opt/hero-quant`）。
- 流程：build/push（镜像 tag = git sha，OCI version 取自 `pyproject.toml`）→ SSH 迁移 →
  SSH 部署 → `/ready` 门 → bluegreen 切流 → 停旧色；deploy 失败自动 `rollback.sh`。
- 迁移 job 失败时**不会**进入 deploy，旧版本继续接流量。

手动：

```bash
export HERO_ENV_FILE=/etc/hero-quant/hero-quant.env
sudo -E ./scripts/deploy/deploy.sh --tag "$(git rev-parse HEAD)"
# 可选：--keep-old 保留旧色 10 分钟，便于秒回滚
```

发布顺序固定：`pull → migrate → up 新色 → /ready → caddy reload → 停旧色`。
任一步失败，`trap` 会把流量切回旧色并写 `deploys.jsonl`（`result=failed`）。

## 4. 蓝绿与回滚

```bash
sudo -E ./scripts/deploy/bluegreen.sh status
sudo -E ./scripts/deploy/bluegreen.sh switch          # 切到另一色（需已健康）
sudo -E ./scripts/deploy/rollback.sh                  # 回上一成功版本（跳到另一色）
sudo -E ./scripts/deploy/rollback.sh --dry-run
```

- 切换只改 `/var/lib/hero-quant/caddy/upstream.caddy` 并 `caddy reload`，应用层中断 < 10s。
- `bluegreen.sh` 同时写 systemd drop-in `10-color.conf`，保证重启后仍按当前颜色拉起。
- **数据库不回滚**：迁移必须向后兼容；跨 DDL 的回滚属人工决策。
- `previous_tag` 只保留一步；更早版本从 `deploys.jsonl` 查 tag 后手动 `deploy.sh --tag`。

## 5. 备份 / PITR / 恢复

- 每日 03:30（systemd timer）：`pg_dump -Fc` + `pg_dumpall --globals-only` +
  `pg_basebackup -Ft -z -X stream` + Redis RDB + `MANIFEST`/`SHA256SUMS`。
- WAL 由 `archive_command` 实时写入 `${HERO_BACKUP_DIR}/wal`（`archive_timeout=60s`）。
- 保留：全备 `BACKUP_KEEP_DAYS=14`、WAL `WAL_KEEP_DAYS=30`；WAL 天数不得小于全备天数。
- 目标值（PR-164 内定）：**RPO ≤ 15 分钟，RTO ≤ 60 分钟**；每月演练一次并记录实测。
- 未演练前，对外只声明"已具备备份脚本"，不得声明恢复时间达标。

```bash
# 逻辑恢复（误删/坏数据；会 --clean 当前库）
sudo -E ./scripts/deploy/restore.sh logical \
  --dump /var/backups/hero-quant/daily/<ts>/heroquant.dump --yes-destroy

# PITR（误操作/逻辑损坏；恢复到指定时间点，旧 PGDATA 保留为 data.old.<epoch>）
sudo -E ./scripts/deploy/restore.sh pitr \
  --base /var/backups/hero-quant/daily/<ts>/pgbase.tar.gz \
  --target-time '2026-10-29 03:00:00+00' --yes-destroy
```

PITR 前提：基线与 WAL 覆盖目标时间；`wal` 目录属主 70:70；恢复期间应用必须停。
恢复脚本会先停 blue/green，再动 PGDATA，最后起回当前颜色并过 `/ready`。

## 6. 监控与日志

- 应用 `/metrics` 只在 compose 内网可达；Caddy 仅允许私网来源访问 `/metrics`。
- pg/redis/otel 均无宿主端口；`docker compose logs -f --tail=200 app-<color>`。
- JSON 日志轮转：每服务 `max-size=20m × 5`；备份/发布记录在 `/var/lib/hero-quant/deploy/`。
- 观测三件套（Prometheus / Alertmanager / Grafana）在 `profile: observability` 下，
  全部只绑宿主回环（9090 / 9093 / 3000）。
  规则清单、为什么这么写、本机校验方式见 [`deploy/monitoring/README.md`](monitoring/README.md)；
  SLO 与错误预算口径见 [`docs/slo.md`](../docs/slo.md)。
- **没接真实告警投递**：Alertmanager 默认接收端是 `unconfigured`，什么都不发（故意的，
  配假 webhook 会让告警静默丢失）。主机/容器/PG exporter 归 PR-301，本期也没有——
  缺它们时告警只覆盖应用层，**没告警不等于没故障**。
  监控配置「验过什么 / 没验什么」见 §8.1。

## 6.1 容量压测与故障演练

```bash
BASE_URL=https://<domain> scripts/perf/smoke.sh          # 前置：端点/证书体检
k6 run scripts/perf/loadtest.js                           # 压测（需 k6）
scripts/perf/fault-drill.sh --list                        # 故障演练清单
scripts/perf/fault-drill.sh --case redis-down --confirm   # 注入→观测→恢复→记录
```

- 台账（基线数值、演练记录）在 [`docs/perf-baseline.md`](../docs/perf-baseline.md)，
  当前全为**待测**：没跑过的场景不许填数字，告警阈值也要回填自那张表。
- 无 LLM key 时压测延迟不具参考性（`_FakeLLM` 接管），`loadtest.js` 会统计
  `degraded_ratio`，报告里先报它。
- 演练默认禁止在生产环境执行（要 `--allow-disruptive-in-prod`），脚本退出前强制恢复。

## 7. 故障排查

| 现象 | 先查 | 处置 |
|---|---|---|
| 502/504 | `bluegreen.sh status`；`docker compose ps` | 上游颜色是否在跑、`/ready` 是否绿；必要时 `switch` 回旧色 |
| `/ready` 503 | `docker compose logs app-<color>` 的 reasons | DSN/Redis 不通按 `docs/runbook.md` §1/§3；不要改探针掩盖 |
| `deploy.sh` 迁移失败 | `migrate.sh` 输出；`MIGRATE_ALLOW_LEGACY_BOOTSTRAP` | W4 未合入时显式开 legacy；不要跳过迁移硬发 |
| Caddy 证书失败 | DNS、80/443、`caddy` 日志 | 域名未解析/端口被占；修好后 `caddy reload` |
| WAL 归档堆积 | `SELECT * FROM pg_stat_archiver;` `ls wal` | 检查 70:70 属主/磁盘；修好后失败 WAL 会自动重试 |
| 备份盘满 | `du -sh daily/*`；保留天数 | 降 `BACKUP_KEEP_DAYS` 前先确认 WAL 覆盖；清理旧目录 |
| 蓝绿切换报未运行 | `docker compose ps app-<target>` | 目标色未部署：先 `deploy.sh --color <target>` |

## 8. 验证状态（诚实边界）

本仓库当前环境只能做静态验证；以下结果可复现：

| 项 | 命令 | 结果 |
|---|---|---|
| compose 解析（blue+green+auth+observability） | `HERO_ENV_FILE=<abs> docker compose --env-file <abs> -f deploy/compose/docker-compose.prod.yml --profile '*' config --services` | 通过（10 个服务） |
| 脚本语法 | `bash -n scripts/deploy/*.sh scripts/perf/*.sh` | 全部通过 |
| 压测脚本 | `node --check`（k6 ESM 语法） | 通过 |
| 监控配置 | YAML/JSON 解析 + 规则结构 + 指标名/label/uid 闭合 | 通过（6 个 yaml + 1 个 dashboard；13 告警 / 9 记录规则）；细则见 §8.1 |
| 告警 PromQL 语义 | `promtool check config` / `promtool check rules` / `amtool check-config` | **未验证**（本机无 promtool/amtool，未拉镜像）——见 §8.1 |
| 版本单源 | `pytest -q tests/test_bootstrap.py` | 2 passed |
| 真实启动/TLS/迁移/备份恢复/蓝绿时延/压测/演练 | 需生产机与域名 | **未验证**（首次上线按第 2 节逐步走，不跳步） |

## 8.1 监控验证状态（2026-10-31）

范围：`deploy/monitoring/**` + `docs/slo.md`。本机 `which promtool` / `which amtool` 都是空，
且按纪律不装包、不拉镜像，所以**分成「静态验过」与「未验证」两栏，不混写**。

**已做（可复现，纯静态）**

| 检查 | 手段 | 结果 |
|---|---|---|
| YAML 可解析 | `yaml.safe_load` × 6（prometheus/alerts/slo/alertmanager/2 个 provisioning） | 通过 |
| Dashboard JSON 可解析 | `json.load` | 通过（8 面板） |
| 规则结构 | 每条 `alert` 有 expr/for/labels/annotations/severity；`for` 是「整数或小数 + ms/s/m/h/d/w/y 单位」；record 名含 `:` | 通过（13 告警 / 9 记录规则，无重名） |
| **指标名闭合** | 从 `src/**/*.py` 提取所有 `Counter/Gauge/Histogram/_get_or_create_*` 注册名（含 prometheus_client 的 `_total`/`_bucket`/`_count`/`_sum` 后缀规则），再比对规则表达式里的每个标识符 | 通过（发现并修掉 1 处：`rerank_fallback_total` → `hero_quant_rerank_fallback_total`） |
| **label 闭合** | 表达式 `by(...)` 的 label 与被聚合指标的 label 集比对（`le` 由直方图提供） | 通过（endpoint/provider/model 均存在） |
| job / 目标闭合 | 规则里的 `job="..."` ⊂ `prometheus.yml` 的 `job_name`；scrape 目标 `app-blue:8899`/`app-green:8899` 在 compose 有同名服务；`alertmanager:9093` 存在 | 通过 |
| rule_files 与挂载一致 | `rule_files: /etc/prometheus/rules/*.yml` ↔ compose 的 `alerts.yml`/`slo.yml` 挂载点 | 通过 |
| Alertmanager 闭合 | route 的 3 个子路由 + 根路由都指向 `receivers` 里存在的 `unconfigured`；`inhibit_rules` 的 `alertname` 是真实告警 | 通过 |
| Grafana | 面板 `datasource.uid=hero-quant-prom` 能在 provisioning 的 datasources 解析到；无 `__inputs`、无 templating 变量；面板查询指标全部存在于 src | 通过 |

**未验证（及原因）**

1. **`promtool check config`（prometheus.yml）**——本机无 promtool，且不拉镜像。它会额外检查
   `rule_files` glob 可加载、`alerting`/`scrape_configs` 的字段拼写、relabel 正则编译。
   目前这些只做了「人工对照 + 服务名闭合」，**不算通过**。
2. **`promtool check rules`（alerts.yml / slo.yml）**——PromQL 的**类型检查**（如
   `histogram_quantile` 参数、`and on()` 的向量匹配、`increase()` 只接计数器）只有 promtool 能判。
   我做的只是「指标名/label 是否存在」的闭合，**不等于表达式语义正确**。
3. **`amtool check-config`（alertmanager.yml）**——`matchers` 语法、空 `receivers` 条目
   （`unconfigured` 没有任何 configs）是否被接受、`inhibit_rules.equal` 的字段名，都需 amtool 确认。
4. **运行时行为**：抓取是否真的成功、告警是否真的从 `pending → firing`、Grafana 面板是否出图，
   都需要起栈（本机不跑 `docker compose up`）。

在有工具的机器上补齐（二选一）：

```bash
# A. 镜像（路径与容器内一致，不用改配置文件）
docker run --rm -v "$PWD/deploy/monitoring:/etc/prom:ro" prom/prometheus:v3.1.0 \
  promtool check config /etc/prom/prometheus.yml
docker run --rm -v "$PWD/deploy/monitoring:/etc/prom:ro" prom/prometheus:v3.1.0 \
  promtool check rules /etc/prom/alerts.yml /etc/prom/slo.yml
docker run --rm -v "$PWD/deploy/monitoring:/etc/am:ro" prom/alertmanager:v0.28.0 \
  amtool check-config /etc/am/alertmanager.yml

# B. 本机二进制（先把文件摆成 rule_files 里的绝对路径布局）
sudo install -d /etc/prometheus/rules
sudo install -m 644 deploy/monitoring/prometheus.yml /etc/prometheus/prometheus.yml
sudo install -m 644 deploy/monitoring/alerts.yml deploy/monitoring/slo.yml /etc/prometheus/rules/
promtool check config /etc/prometheus/prometheus.yml
promtool check rules /etc/prometheus/rules/alerts.yml /etc/prometheus/rules/slo.yml
amtool check-config deploy/monitoring/alertmanager.yml
```

**`unconfigured` 的行为（必须明确说清）**

Alertmanager 的根路由和三个 severity 子路由都指向 `unconfigured`，这个 receiver **没有配任何
`webhook_configs`/`email_configs`**，所以它 **不投递任何消息**——不发给群、不发邮件、不发工单。
告警只出现在两个地方：Prometheus 的 `/alerts` 页、Alertmanager 的 UI（9093，仅宿主回环）。
这是**故意**的：配一个假 webhook 会让告警静默丢失，比"看得见没人收"更危险。
要真正投递，解开 `alertmanager.yml` 里 `im-webhook` 注释段、填地址（地址不进仓库），
再把路由 receiver 改过去。

**已发现并修复的不一致（compose 旗标拼写）**

`deploy/compose/docker-compose.prod.yml:186,190` 原先把 Prometheus 的存储旗标
`--storage.tsdb.path` / `--storage.tsdb.retention.time` 里的 `tsdb` 误写成三个字母相近的 `tsv`。
按原写法 Prometheus 会以 `unknown long flag` 直接退出，整个观测栈起不来。
**已修**：只改这两个旗标（`tsv` → `tsdb`），端口/卷/profile/资源限制一律未动；
改动已落在工作区，**commit 待总管统一提交**。（`grep -rn "storage\.tsv"` 现在应为空。）

## 9. 安全要点

- 只有 caddy 暴露宿主端口；PG/Redis/app 全部内部网络（`internal: true` 禁出网）。
- app 容器：非 root、`read_only`、`cap_drop ALL`、`no-new-privileges`、tmpfs。
- 密钥只在 env 文件与 GitHub secrets；`backup.sh` 产物 700，`deploys.jsonl` 600。
- `/metrics` 公网 403；Caddy 去 `Server` 头 + HSTS/`nosniff`/`DENY`。
## 队列 worker 与租约回收（批次15）

| 单元 | 作用 | 启停 |
|---|---|---|
| `hero-quant-runs-worker.service` | PG 队列消费者（`python -m hero_quant.runs --handler pkg.mod:func`） | `systemctl enable --now hero-quant-runs-worker` |
| `hero-quant-runs-reap.service` + `.timer` | 每 30s 回收过期租约（`--reap`），超 max_tries 置 dead | `systemctl enable --now hero-quant-runs-reap.timer` |

前置：
1. `/etc/hero-quant/hero-quant.env` 里必须有 `HERO_RUNS_HANDLER=pkg.module:function`（不给就 exit 2，不会空转领任务）；
2. 队列 DSN 用 `HERO_QUEUE_WORKER_DSN`（角色 `hero_queue_worker`，见 `docs/SECURITY-SECRETS.md` §7）；
3. 管理后台只读用 `HERO_ADMIN_READONLY_DSN`（角色 `hero_admin_readonly`）。

停 worker：`systemctl stop hero-quant-runs-worker` —— SIGTERM 是协作式停止，当前任务跑完才退出；
卡住超过 `TimeoutStopSec=120` 会被强杀，未完成的租约由 reaper 在到期后打回 queued。
