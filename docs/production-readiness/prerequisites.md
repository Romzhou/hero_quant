# 生产化准备清单（非代码部分）

> 用途：除了写代码，你需要提供的**环境、账号、密钥、预算、人员、数据与合规材料**。
> 依据：`README.md` 的 D1–D7 决策（内部团队 / 研究回测 / 自建 K8s / 全功能分期 / Keycloak / 多 provider / 公开源内部用）。
> 标注：`P1` = 第一期必须；`P2` = 第二期；`P3` = 第三期。

---

## 0. 先看结论：你现在最缺的 6 样

| # | 缺什么 | 现状 | 影响 |
|---|---|---|---|
| 1 | **开发机算力/磁盘** | 当前 4 vCPU / 8GB RAM / 49GB 磁盘仅剩 1.7GB | 跑不动 K8s/PG/压测；镜像与构建随时 ENOSPC |
| 2 | **LLM API Key 与预算** | `.env` 无有效 key，真模型一次未跑 | Phase 1 的评测基线无法建立 |
| 3 | **Keycloak 域名/TLS/管理员** | 无 | 登录与租户隔离无法开工 |
| 4 | **K8s 集群与镜像仓库** | 只有单机 compose | 云托管部署无法开始 |
| 5 | **PG/Redis 生产实例** | 本机 docker 容器 | 备份/PITR/HA 无从谈起 |
| 6 | **领域专家标注的 golden set** | 只有 38 条 fake 评测 | 质量无法度量，模型/提示词改动是盲调 |

---

## 1. 硬件与环境

### 1.1 开发环境

| 项 | 最低 | 推荐 | 说明 |
|---|---|---|---|
| CPU | 8 核 | 16 核 | 并行跑 pytest/前端构建/容器 |
| 内存 | 32GB | 64GB | PG/Redis/后端/前端同时起 |
| 磁盘 | 200GB SSD，空闲 ≥100GB | 500GB NVMe | Docker 镜像、构建缓存、数据集 |
| 网络 | 稳定出网 443 | 同上 | 拉依赖、LLM/数据 API |
| GPU | 不需要 | 可选 1× L4/A10 24GB | 仅当自托管 embedding/rerank；用 API 则不需要 |

**当前机器评估**：4 vCPU / 7.8GB / 1.7GB 可用 —— 只够改代码和跑单测，**不够** Phase 1 的容器化联调、PG 真跑、压测。建议先扩磁盘到 ≥100GB 空闲，或准备一台独立开发机。

### 1.2 staging（第一期）

| 组件 | 规格 | 数量 | 用途 |
|---|---|---|---|
| K8s 节点 | 4C16G / 100GB SSD | 3 | API、前端、Keycloak、监控 |
| PostgreSQL | 4C16G / 100GB SSD | 1（可单机） | checkpoint/billing/session/audit |
| Redis | 2C8G | 1 | 限流、票据、缓存 |
| 对象存储 | 100GB | 1 bucket | 回测产物、trace、快照 |
| 出口带宽 | ≥50Mbps | — | LLM/数据 API |

### 1.3 prod（第三期前到位）

| 组件 | 规格 | 数量 | 用途 |
|---|---|---|---|
| K8s 节点 | 8C32G / 200GB SSD | 3–5（跨 AZ） | API/worker/前端/监控 |
| PostgreSQL | 8C32G / 500GB NVMe，主备 | 1 套 | 生产数据，HA |
| Redis | 4C16G，主从 | 1 套 | 生产缓存/限流 |
| 对象存储 | 1TB + 版本化 | 1 bucket | 产物/快照/备份 |
| 备份存储 | 1TB | 1 | PG 全备 + WAL 归档 |
| 监控存储 | 4C16G / 200GB | 1 | Prometheus/Grafana/Loki/Tempo |
| GPU | 不需要 | — | 多 provider 走 API；自托管模型才需要 |

> 规模假设：内部 5–30 人、日问答几百到几千次、回测以批量为主；不是公网 SaaS 量级。若用户/流量更大，按压测结果扩容（Phase 3 PR-302）。

### 1.4 网络与域名

- 一个公网域名（如 `quant.example.com`）+ TLS 证书（或 cert-manager 自动签发）。
- Ingress/LoadBalancer 入口；PG/Redis **不得**公网暴露。
- 出网白名单：LLM provider、行情/数据源、对象存储、镜像仓库、企业 IM。
- 如团队远程访问：VPN/零信任网关（当前 tailscale 仅适合开发）。

---

## 2. 账号与访问权限

### 2.1 第一期（P1）

| 账号/权限 | 用途 | 谁提供 | 备注 |
|---|---|---|---|
| Keycloak 管理员 + 域名 + TLS | 登录/OIDC | 你/运维 | 自建，部署在 K8s |
| K8s kubeconfig（staging，namespace 级） | 部署/排障 | 你/运维 | 不给 prod 管理员 |
| 镜像仓库 push | CI 构建 | 你/运维 | 建议 Harbor/云 registry |
| PG/Redis DSN（staging） | 应用连接 | 你/运维 | 应用账号 + 迁移账号分离 |
| LLM API Key（多 provider） | 真模型 | 你 | 配 spend limit，存 Secret Manager |
| Secret Manager（Vault/KMS/云 SM） | 密钥托管 | 你/运维 | 生产禁止 `.env` |
| 企业 IM Webhook（飞书/企微/Slack） | 告警/审批 | 你 | 一个群即可 |
| GitHub/GitLab 仓库权限 + Actions secrets | CI/CD | 你 | 不要给个人 token |
| 对象存储 bucket + AK/SK | 产物/快照 | 你/运维 | 最小权限 |
| 域名/DNS 管理 | 入口 | 你 | — |

### 2.2 第二期（P2）

- Cohere API Key（或确认用本地 rerank）；pgvector 所在 PG 的 DSN。
- Temporal 运行环境（自建或 Temporal Cloud）与命名空间。
- 渗透测试供应商/预算；测试环境授权书。
- 企业内部用户清单/租户映射（谁属于哪个团队）。
- Billing 内部结算规则（额度、价格、审批人）。

### 2.3 第三期（P3）

- 生产 K8s/DB/Redis 的只读/发布权限（仍建议只给 CI，不给个人）。
- 压测环境（独立，避免打生产）。
- 多地域/DR 资源（如需要）。
- 法务/合规评审：隐私政策、数据保留、数据源授权。
- 红队/渗透复测预算。

---

## 3. 密钥与凭据（怎么给，不给什么）

### 3.1 正确给法

1. 生产密钥只进 **Secret Manager / K8s Secret**，由 CI/CD 注入。
2. 发给 agent 的只能是**测试/ staging** 凭据，且设最小权限、可随时吊销。
3. Key 命名与轮换周期写入 runbook；轮换不影响服务。
4. LLM key 设月度 spend limit；数据源 token 设 IP/额度限制。

### 3.2 红线（不要在聊天/文档/仓库里给）

- 生产 root/超级用户密码、个人支付卡、客户 PII、未授权数据。
- 长期不过期的个人 access token。
- 与工作无关的个人账号密码。

> 现有 `.env` 里的真密钥只轮换、不传播（AGENTS §2）。

---

## 4. 第三方服务与预算

| 项 | 第一期 | 第二期 | 第三期 | 备注 |
|---|---|---|---|---|
| LLM tokens | 必须 | 必须 | 必须 | 先定月度上限，80% 告警 |
| K8s/计算 | 必须 | 必须 | 必须 | staging 可小；prod 按 HA |
| PG/Redis | 必须 | 必须 | 必须 | 托管或独立部署 |
| 对象存储/备份 | 必须 | 必须 | 必须 | 版本化 + 跨区可选 |
| Keycloak | 必须 | — | — | 自建，无 license |
| 企业 IM | 必须 | 必须 | 必须 | 告警/审批 |
| Cohere/rerank | 可选 | 建议 | 建议 | 或本地 cross-encoder |
| Temporal | — | 建议 | 必须 | 自建或 Cloud |
| 数据源（tushare token 等） | 必须 | 必须 | 必须 | 公开源免费；商业源另计 |
| 渗透测试 | — | 必须 | 复测 | 按次报价 |
| 监控存储 | 建议 | 必须 | 必须 | 指标/日志/追踪 |

**预算口径**：先给 LLM 月度上限与云资源月度上限两个数字；成本告警和租户配额都依赖它们（PR-105/PR-311）。

---

## 5. 数据与授权

| 项 | 第一期 | 说明 |
|---|---|---|
| 公开源使用范围确认 | 必须 | akshare/tushare/yahoo 仅内部研究，不对外分发；写入产品文档 |
| tushare token | 如使用 | 注册后配置到 Secret |
| 历史行情覆盖范围 | 必须 | 至少 3–5 年日线，用于 PIT/回测 |
| 复权/停牌/退市规则 | 必须 | 数据源文档或采购说明 |
| golden set 标注 | 必须 | 50–100 条 query + 期望工具/答案/拒答，需领域专家 |
| 商业数据源合同 | P2/P3 | 对外提供结论前完成（Wind/聚源/同花顺等） |
| 数据保留/删除政策 | P3 | 法务确认 |

> 没有 golden set，Phase 1 的“真模型评测”只能跑通、不能判好坏。

---

## 6. 人员与组织

| 角色 | 职责 | 投入 |
|---|---|---|
| 决策人（你） | 范围/预算/优先级/验收 | 关键节点 |
| 产品负责人 | 需求、验收、用户反馈 | 持续 |
| 后端工程师 | API/Agent/数据/持久化 | 1–2 人 |
| 前端工程师 | 登录/页面/交互/e2e | 1 人 |
| 平台/SRE | K8s/CI/CD/备份/监控/on-call | 0.5–1 人 |
| 数据工程师 | PIT/快照/质量/评测数据 | 0.5–1 人 |
| 安全评审 | 渗透/权限/密钥 | 阶段投入 |
| 领域专家 | golden set 标注、结果验收 | 阶段投入 |
| 内部测试用户 | 5–20 人试用 | Phase 1 末 |

**On-call**：至少 1 名主备；runbook 在 G1 前完成。

---

## 7. 非代码资产

- **产品文案**：登录页说明、免责声明、隐私政策、数据来源与限制、错误提示文案。
- **品牌**：logo/配色（可选；现有 UI 可先用）。
- **接口契约**：如果内部还有其他系统要对接，提供 OpenAPI/字段规范。
- **用户与租户清单**：邮箱、所属团队、角色（viewer/analyst/admin）。
- **审批规则**：哪些动作需要谁批准（Phase 2）。
- **运维制度**：发布窗口、回滚审批、故障升级路径。

---

## 8. 开工前 10 项 Checklist（Phase 1）

- [ ] 开发机：≥8C/32G/100GB 空闲（当前 1.7GB 不够）。
- [ ] LLM key（多 provider）+ 月度预算上限。
- [ ] Keycloak 域名 + TLS + 管理员。
- [ ] staging K8s 集群 + kubeconfig + 镜像仓库。
- [ ] staging PG/Redis/对象存储 + DSN。
- [ ] Secret Manager（Vault/KMS/云 SM）。
- [ ] 企业 IM 告警群 + Webhook。
- [ ] 50–100 条 golden query 与标注人。
- [ ] 内部测试用户清单（5–20 人）与租户映射。
- [ ] 免责声明/隐私政策/数据源限制文本（可先草稿）。

---

## 9. 你不需要准备的东西

- 不需要自建 GPU 集群（多 provider 走 API）。
- 不需要公网开放注册/支付（D1 内部工具）。
- 不需要券商接口/交易通道（D2 不接实盘）。
- 不需要把生产 root 密码交给 agent（用 CI/CD + Secret Manager）。
- 不需要一次买齐 Phase 2/3 的资源（按 G1/G2 门逐步到位）。

---

## 10. 按期的准备节奏

| 期 | 你需要到位 | 可以晚点 |
|---|---|---|
| Phase 1 | 开发机、LLM key、Keycloak 域名、staging K8s、PG/Redis、Secret Manager、IM、golden set、测试用户 | prod HA、商业数据、渗透 |
| Phase 2 | Cohere/pgvector、Temporal、渗透供应商、Billing 规则、更多测试用户 | 多地域 DR |
| Phase 3 | prod HA/DR、压测环境、数据授权/法务、红队复测、on-call 制度 | — |
