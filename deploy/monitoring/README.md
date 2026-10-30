# 监控与告警（PR-302/303 骨架）

> 交付范围：Prometheus 配置 + 告警规则 + Grafana 看板供给 + Alertmanager 路由骨架
> + SLI/错误预算记录规则。对应 PR-142（看板）/143（告警路由）/144（SLO）。
> **PR 编号校正**：任务卡写的是 "PR-182/183"，但 backlog 里 PR-182 是
> 「OpenAPI 契约测试」（W10，后端），与监控无关；W5 观测是 PR-141~144。
> 我按内容做（PR-142/143/144），编号以 backlog 为准。
>
> 仍缺的部分：主机/容器/数据库 exporter（PR-301）、真实告警投递通道（需 webhook 地址）、
> 自动 SLO 报告（PR-313）。缺前两项时告警只覆盖应用层，
> **不要因为没告警就认为没故障**。

## 1. 启用

```bash
export HERO_ENV_FILE=/etc/hero-quant/hero-quant.env
docker compose --env-file "$HERO_ENV_FILE" \
  -f deploy/compose/docker-compose.prod.yml \
  --profile blue --profile observability up -d prometheus alertmanager grafana

# 全部只绑宿主回环：
#   Prometheus    http://127.0.0.1:${HERO_PROM_PORT:-9090}   Status → Rules / Alerts
#   Alertmanager  http://127.0.0.1:${HERO_AM_PORT:-9093}
#   Grafana       http://127.0.0.1:${HERO_GRAFANA_PORT:-3000}（admin / GRAFANA_ADMIN_PASSWORD，登录后立即改）
```

Grafana 的数据源与看板都是**仓库内文件自动供给**（provisioning 目录只读挂载），
不用在 UI 里手点；改看板就改仓库里的 JSON，别在 UI 里保存（`allowUiUpdates: false`）。

Alertmanager 的默认接收端是 `unconfigured`——**它什么都不发**。这是故意的：
配一个假 webhook URL 会让告警静默丢失。要投递就把 `alertmanager.yml` 里
`im-webhook` 那段解开并填地址（地址不进仓库）。

文件位置：

| 文件 | 作用 | PR |
|---|---|---|
| `prometheus.yml` | 抓取配置（app 蓝绿两色 + 自身）+ 指向 alertmanager | PR-141/143 |
| `alerts.yml` | 告警规则（12 条） | PR-143 |
| `slo.yml` | SLI/错误预算记录规则 + 预算耗尽告警 | PR-144 |
| `alertmanager.yml` | 告警路由骨架（**默认接收端不投递**） | PR-143 |
| `grafana/provisioning/**` | 数据源与看板自动供给 | PR-142 |
| `grafana/dashboards/hero-quant-overview.json` | 看板：QPS/p95/错误率/熔断/成本/预算 | PR-142 |

## 2. 抓取范围与已知盲区

抓取到的是应用 `/metrics`（Prometheus 文本格式），包含：

- `hero_quant_requests_total{endpoint}`、`http_request_duration_seconds{endpoint}`（histogram，含 `_bucket`）
- `hero_quant_api_errors_total{endpoint,error_code}`
- `circuit_state`（无 label，0/1/2）、`data_source_circuit_open_total{source}`
- `hero_quant_llm_retry_total{provider,reason}`、`hero_quant_llm_timeout_total{provider}`
- `hero_quant_llm_tokens_total{provider,model,kind}`、`hero_quant_llm_cost_total{provider,model}`
- `hero_quant_llm_latency_seconds{provider,model}`（histogram）、`hero_quant_llm_failover_total{src,dst,reason}`
- `hero_quant_governance_wall_time_exceeded_total{operation}`、
  `hero_quant_governance_ledger_append_total{tenant,status}`、
  `hero_quant_governance_ledger_append_duration_seconds{tenant,status}`
- `hero_quant_risk_errors_total{op,error_code}`、`hero_quant_rerank_fallback_total`（无 label）
- `hero_quant_wall_time_seconds{operation,status}`、`hero_quant_governance_dedup_op_total{op,status}`

> 名字以 `src/hero_quant/metrics/__init__.py` 及各模块的构造点为准（2026-10-31 逐个核对过）；
> `rerank_fallback_total` 这类**少了 `hero_quant_` 前缀**的写法会永远匹配不到序列，属静默失效。

**盲区**（需要 PR-301 补 exporter，配置里已留注释好的 job）：

- 主机磁盘/内存/CPU → `node_exporter`（磁盘告警、OOM 排查都依赖它）
- PG 连接数/复制延迟/WAL 归档失败 → `postgres_exporter`
- 容器级资源 → `cadvisor`
- 公网可达性与证书到期 → 黑盒探测 / `scripts/perf/smoke.sh`（本期用 smoke 顶着）

## 3. 告警清单与为什么这么写

| 告警 | 表达式要点 | 备注 |
|---|---|---|
| `HeroQuantAllAppInstancesDown` | `(sum(up{job=...}) == 0) and (count(up{job=...}) > 0)` | 蓝绿下**必须**用聚合式：单实例 `up==0` 在发布/停色时必然误报；`count>0` 区分"没抓到"和"全挂" |
| `HeroQuantHighLatencyP95` | `histogram_quantile(0.95, ...)` > 5s / 10m | 阈值待首次基线回填，先用保守值 |
| `HeroQuantApiErrorRateHigh` | 错误率 > 5%，分母 `clamp_min` | 429/503 属于限流与降级，先看 `/ready` reasons 再定性 |
| `HeroQuantCircuitBreakerOpen` | `circuit_state >= 2` | 熔断是**预期可见**的降级；持续过长才需要处理 |
| `HeroQuantDataSourceCircuitOpeningOften` | 30min 内 > 3 次 | 反复开合说明阈值过紧或上游不稳 |
| `HeroQuantMemoryRerankFallbackSpike` | 15min 内 > 10 次 | 向量不可用会回退纯 Python 重排；功能不中断，只是观测信号 |
| `HeroQuantLLMProviderFailoverSpike` | 10min 内切换 > 3 次 | 先于 `AllProvidersFailed` 报错发现抖动 |
| `HeroQuantLLMTimeoutSpike` | 10min 内超时 > 5 次 | 弱网/流式更容易命中，看 provider 状态与超时预算 |
| `HeroQuantLLMLatencyP95High` | `provider/model` 维度 p95 > 30s / 15m | deep 模型长思考是可能的，持续超标要调槽位 |
| `HeroQuantLLMDailyCostExceedsBudget` | 近 24h 成本 > 100 USD | **占位阈值**，按真实预算改 `alerts.yml` 里的数值 |
| `HeroQuantWallTimeBudgetExceeded` | 15min 内 > 0 | 治理超时不算故障，但是调优信号 |
| `HeroQuantTrafficDropToZero` | 北京时间 09:00–23:00 内 30min 零请求 | `hour()` 是 **UTC**，所以表达式写 `hour() >= 1 and hour() < 15`；照抄 `hour() >= 9` 会在北京夜里报、白天静默。内部工具没人用时也可能真没人用，先看 `/ready` 别急着定故障 |

改阈值直接改 `deploy/monitoring/alerts.yml`，改完：

```bash
curl -X POST http://127.0.0.1:9090/-/reload   # 需要 --web.enable-lifecycle（已开）
```

## 4. 校验方式（本机已能做的）

```bash
python3 -c "
import yaml, json
base='deploy/monitoring/'
for f in ['prometheus.yml','alerts.yml','slo.yml','alertmanager.yml',
          'grafana/provisioning/datasources/prometheus.yml',
          'grafana/provisioning/dashboards/hero-quant.yml']:
    yaml.safe_load(open(base+f)); print('yaml ok:', f)
d = json.load(open(base+'grafana/dashboards/hero-quant-overview.json'))
print('dashboard ok: panels =', len(d['panels']))
"
python3 - <<'PY'
import yaml, glob
alerts = records = 0
for f in ['deploy/monitoring/alerts.yml', 'deploy/monitoring/slo.yml']:
    d = yaml.safe_load(open(f))
    for g in d['groups']:
        for r in g['rules']:
            if 'alert' in r:
                alerts += 1
                for k in ('expr', 'for', 'labels', 'annotations'):
                    assert k in r, (r['alert'], k)
                assert r['labels'].get('severity') in ('critical', 'warning', 'info'), r['alert']
            else:
                records += 1
                assert 'expr' in r
print(f'rules ok: alerts={alerts} records={records}')
PY
```

实跑结果（本仓库，2026-10-31 复跑）：
`yaml ok` × 6、`dashboard ok: panels = 8`、`rules ok: alerts=13 records=9`；
`docker compose --profile '*' config --services` 渲染出 10 个服务，观测三件套端口只绑 `127.0.0.1`。
指标名/label/uid/route 的闭合校验脚本与结论见 `deploy/README.md` §8.1「监控验证状态」。

## 5. SLO 与错误预算

口径、记录规则、冻结策略在 [`docs/slo.md`](../../docs/slo.md)。

一个容易踩的坑要提前说：SLO 规则用 **30 天窗口**。生产 compose 与 `prod.env.example`
的 `HERO_PROM_RETENTION` 默认已是 35d，但 `staging.env.example` / `dev.env.example`
是 3d / 1d——按那些值起 Prometheus 时 `heroquant:error_budget_remaining:ratio30d`
会给出偏小甚至空的值，**看起来像 SLO 很好，其实是窗口不够**。
要真算月度 SLO 就把保留期提到 35d 并确认磁盘（`preflight.sh` 在 <30d 时打 WARN）。

## 6. 未做的事

没有用 `promtool check rules` / `amtool check-config` 跑过（本机无这两个工具，
且不想为了校验拉几百 MB 镜像），PromQL 是照已注册的指标名手写的。首次接线请在目标机上补：

```bash
# 本机没有 promtool/amtool；下面在「有工具的机器」上跑（用镜像只是一种取证方式）
docker run --rm -v "$PWD/deploy/monitoring:/etc/prom:ro" prom/prometheus:v3.1.0 \
  promtool check config /etc/prom/prometheus.yml        # 含 rule_files / alerting 段
docker run --rm -v "$PWD/deploy/monitoring:/etc/prom:ro" prom/prometheus:v3.1.0 \
  promtool check rules /etc/prom/alerts.yml /etc/prom/slo.yml
docker run --rm -v "$PWD/deploy/monitoring:/etc/am:ro" prom/alertmanager:v0.28.0 \
  amtool check-config /etc/am/alertmanager.yml
```

装好原生二进制的机器上等价写法（更快，不拉镜像）。注意 `prometheus.yml` 里的
`rule_files` 是**容器内绝对路径** `/etc/prometheus/rules/*.yml`，所以要先摆成同样的目录：

```bash
sudo install -d /etc/prometheus/rules
sudo install -m 644 deploy/monitoring/prometheus.yml /etc/prometheus/prometheus.yml
sudo install -m 644 deploy/monitoring/alerts.yml deploy/monitoring/slo.yml /etc/prometheus/rules/
promtool check config /etc/prometheus/prometheus.yml
promtool check rules /etc/prometheus/rules/alerts.yml /etc/prometheus/rules/slo.yml
amtool check-config deploy/monitoring/alertmanager.yml
```

这一步没跑之前，规则文件只能算"YAML 可解析 + 结构与指标名/label 已核对"，
**不算 promtool 语义验证通过**（PromQL 类型检查、`for` 时长解析、record 名冲突检查都在 promtool 里）。
另外 **没有真实告警投递**：Alertmanager 接收端是 `unconfigured`，它什么都不发。
