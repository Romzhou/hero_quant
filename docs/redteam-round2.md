# 对抗审查 Round2（M 线：bug / 攻击者 / 新人三视角重审）

- 日期：2026-10-19；基线：`a27e618`（M 起点）→ `b7c697a`（收尾时树顶；其间 J/K/F/D/L 并发落子 6 笔，本报告只读未碰）。
- 权限声明：禁 push（未推）；`.env` 真值未读未贴；`migrations/` 未碰；禁区文件（`server.py` 等）只读；
  J/K 当期文件（近 8 笔提交动过的 47 个 src 文件）一律只记账不修。
- 工作树现状：收尾时 `git status` 见他人未提交改动（`api/security.py` 约164行、`api/server.py` 约6行，系 D 线 G-08 scope 分域进行中），
  M 线未碰、未提交这些文件；本报告行号凡涉该两文件以“已提交树 + 工作树观测”双口径标注。
- 前序输入：`docs/recon.md`、`docs/issues.md`、`docs/security-audit.md`（G-01~G-13）、`docs/runbook.md`。

## 结论速览（视角×定级表）

| 视角 | 高 | 中 | 低 | 信息/通过 |
|---|---|---|---|---|
| ① bug 逻辑错 | 0 | 1（M2-04 记账） | 0 | 3 修（M2-01~03）+ 1 复核通过（M2-05） |
| ② 攻击者（含 G-02 复核） | 0 | 2（M2-06 残余记账、M2-09 记账） | 2（M2-08、M2-10 记账） | 2 复核通过（M2-07 观察、M2-11 观察） |
| ③ 新人实跑 | 0 | 0 | 2（M2-15 文档漂移记账） | 4 通过（M2-12~14，见 §3） |

- 修了 3 条（3 个独立 `M: ...` commit，见 §4）；记账 8 条；复核通过/观察 3 条；合计发现 14 项。

---

## §1 bug 视角（逐模块逻辑错）

### M2-01 checkpoint `_redact_dsn` 漏空用户名 `:pass@` 形态 —— 修
- 文件+行号：`src/hero_quant/checkpoint/postgres.py:237-243`（修前）
- 视角：bug；定级：中（口令明文进日志；触发需空用户名 DSN，但属 PII 泄漏面）
- 依据：正则 `://([^:]+):[^@]*@` 要求用户名非空，与 `src/hero_quant/config/settings.py:37-45`（已覆盖两种形态）分叉；
  同类问题 G-09 在 `server.py` 侧仍记账未修，本文件非碰撞、可单点对齐。
- 处置：**修**（commit `76ebc93`）：正则改为 `://([^/@]*):[^@]*@` 与 settings 同构，补 `isinstance/"://"` 守卫，
  `except Exception` 收窄为 `(TypeError, ValueError, AttributeError)`。已验：`:s3cr3t@`→`:***@`，有用户名形态不变，无口令 DSN 不变。

### M2-02 `risk_summary` degraded 无 reason —— 修
- 文件+行号：`src/hero_quant/api/risk.py:307-339`（修前 `337-338` 仅置 `degraded:true`）
- 视角：bug；定级：中（AGENTS §6：`degraded:true + reason` 为强制语义；无因降级=假成功的一半）
- 处置：**修**（commit `4ad6de1`）：降级时逐项追加 `reason`（`turnover_missing/exposure_missing/…/circuit_unknown/pit_unknown/cross_source_unknown`，
  `;` 连接，全空回退 `risk_probe_degraded`）。行为变更仅“加字段”，`tests/test_api_risk_real.py` 2 passed 已复核。

### M2-03 `issue_ticket` ttl<=0 返回永不可消费的死票且静默 —— 修（可观测）
- 文件+行号：`src/hero_quant/api/security.py:172-175`（修前无任何日志）
- 视角：bug；定级：低（调用方恒传 60s，生产难触发；但签发 200 + 消费恒 False 的组合属静默陷阱）
- 处置：**修**（commit `a27e618`）：分支内加 `logger.warning("security.ticket_issued_immediately_expired ttl=%r")`，
  零语义变更（旧契约“签发即过期”保留，防单测/调用方断裂）。`tests/test_sse_ticket.py` 4 passed 已复核。

### M2-04 billing 真 PG 购买 `tenant` 列仍填 buyer，卖家归属丢失 —— 记账
- 文件+行号：`src/hero_quant/billing/service.py:868`（emulated 收据 `"tenant": buyer_tenant`）、
  `:950`（PG 收据同）、`:1011`（`_tenant = buyer_tenant or tenant or "default"`）、`:768`（`get_factor(factor_id, tenant=buyer_tenant)`）
- 视角：bug；定级：中（G-03 残余变形：schema 已从复合 FK 切为 `UNIQUE (factor_id, buyer_tenant)`（`:101`），
  跨租户购买在 `:768` 即按买方域查不到 factor 而 fail-closed——可用性不断，但卖家经 `purchases_seller_select`（`:126`，`tenant=current_tenant()`）永远看不见售出单，财务归因仍断）
- 处置：**记账 → billing 专线**（J/K 当期文件，不碰）。修复方向：purchase 解析 factor 的 seller tenant 写入 `tenant` 列，
  `SET LOCAL` 仍用 buyer 以满足 `purchases_insert`（`:128`）。

### M2-05 G-04/G-05 已修复核通过 —— 无需处置
- 文件+行号：`src/hero_quant/billing/service.py:646-647`（factor 补偿删双写 SET LOCAL）、`:1117-1118`（purchase 补偿删双写）、`:218`（`_billing_dsn_from_env` 与 settings 同门禁注释）
- 视角：bug（复核）；定级：信息
- 处置：**复核通过**，F 线已修（碰撞文件，M 未动）。round2 抽核：补偿删除双键 SET LOCAL 与 `_pg_insert_purchase_sync`（`:1028-1042`）对齐一致。

---

## §2 攻击者视角（含 G-02 修后复核）

### M2-06 G-02 修后复核：匿名票强制绑定 + per-connection 频道真闭合 —— 通过（附 1 中危残余记账）
- 文件+行号：
  - 发侧：`src/hero_quant/api/server.py:1668-1675`（匿名票 `?user=` 丢弃 + warning + `_bound_user=""`，D: `7dfc9fa`）
  - 订阅侧：`src/hero_quant/api/ws.py:103-106`（匿名票 `resolve_user_channel(None, …)` per-connection，F: `8c200ba`）、
    `:101`（绑定票声明不一致返回 `(None, reason)`）、`:422-423`/`:486-487`（mismatch 4001 关闭）
- 判定：**匿名票冒充他人 `ws:channel:{user}` 已双侧闭合**（发侧恒 `""` + 订阅侧忽略 `?user=`，双保险任一即成立）。
  `tests/test_sse_ticket.py` 4 passed 佐证无回归。
- 残余（记账，中）：已鉴权调用方的 `?user=` 仍为同共享密钥持有者互信声明——`server.py:1650` `_bound_user = _claimed_user`，
  代码注释自认“真 per-user 隔离需 per-user 密钥身份（后续专线）”。即：任一持 key 者仍可签任意 user 的绑定票并订阅其频道。
  在单共享密钥架构下属已知设计债，非本次回归。**记账 → D/鉴权重构专线**（`server.py`/`ws.py` 双禁区+双碰撞，不碰）。

### M2-07 G-08 票据 scope 分域 —— 复核通过（观察：他人未提交改动）
- 文件+行号（工作树观测，提交者自测为准）：`src/hero_quant/api/server.py:1298`（`/metrics` 要求 `require_scope="metrics"`）、
  `:1332`（query 要求 `scope="query"`）、`:1647`（非法 scope 400）、签发端 `scope=` 透传（`issue_ticket(..., scope=_ticket_scope)`）
- 视角：攻击者；定级：信息
- 处置：**复核通过/观察**——D 线 G-08 改动正在他人工作树中（未提交，M 未碰）。提醒：legacy 无分域票迁移期放行窗口需设关闭时间，否则分域=纸面；
  `consume_ticket_with_user` 的 `require_scope` 语义需单测锁定（届时 B 线）。

### M2-08 HMAC 显式 secret 参数被 env 静默遮蔽 —— 记账
- 文件+行号：`src/hero_quant/api/security.py:358`（`secret_env = os.environ.get("HERO_HMAC_SECRET", "") or secret or ""`）
- 视角：攻击者；定级：低（调用方显式传 secret 做隔离/轮换时，env 存在即被无声覆盖，易致“以为换钥实则未换”）
- 处置：**记账**（safe 文件但属鉴权语义变更，需 D 线评估调用方契约，M 不擅改）。

### M2-09 `os.environ.get` 绕过 `test_config` 门禁字面检查 —— 记账
- 文件+行号：门禁 `tests/test_config.py:13-17`（仅断言 `"os.getenv"` 字面不在非 config 文件）；
  调用方 `src/hero_quant/api/security.py`（`os.environ.get` ×6：`:274,358,471,478,492` 等）、`src/hero_quant/security/credentials.py:136` 等
- 视角：攻击者（配置面审计盲区：README:110 宣称“全仓仅 settings.py 允许 os.getenv”，但 `os.environ.get` 等价物散落安全边界文件而不被门禁捕获）
- 定级：中（env 网关唯一性被稀释；`HERO_HMAC_SECRET/HERO_API_KEY` 读取点游离于 settings 审计之外）
- 处置：**记账 → B 线**（门禁在 `tests/`，M 禁区；修复=门禁改查 `os.environ` 或收敛读取点，需 B/D 合议）。

### M2-10 票据 TTL>3600 静默收敛 —— 记账
- 文件+行号：`src/hero_quant/api/security.py:164-165`（`if ttl_int > 3600: ttl_int = 3600`，无日志无计数）
- 视角：攻击者；定级：低（调用方请求长票被静默截断；反向看是上限保护，方向正确，仅缺可观测）
- 处置：**记账**（一行 warning 即可，留给 D 线顺手；M 本轮额度让给 M2-01~03 更硬的项）。

### M2-11 G-06/G-07 K 线并发已修 —— 观察
- 文件+行号：commit `67d27d5`（G-06 签票限流 fail-closed 503）、`b7c697a`（G-07 限流键混入 tenant）
- 视角：攻击者；定级：信息
- 处置：**观察**——与本轮并发落地，未及复核 diff；请 K 线自带单测证据，总管验收时抽核。

---

## §3 新人视角（README + runbook 从零实跑）

### M2-12 启动实测 —— 通过
- `python -c "import hero_quant.api.server"`（src 下）：OK。唯一告警为 `mcp/server.py:200` FastMCP 缺失走本地 stub（预期内降级，有日志）。
- `uvicorn hero_quant.api.server:app --port 8899` 实起：`GET /live` → 200 `{"status":"ok"}`；
  `GET /ready` → degraded（503 语义，Invoke-WebRequest 按 503 抛错为**预期**）：body 含
  `degraded:true + reasons{checkpoint PG unreachable…fail-closed(memory), billing DSN 缺配…(memory), cohere unhealthy}`。
  结论：P1-2 emulated 打标已治（内存双降级如实声明，无假绿）。实测后 job 已停、端口已让出。
- `frontend/dist` 存在（3 entries）：P0-3 零构建假活在本工作区不触发；`server.py:2485` `_dist_path` 正常拾取（import 日志 `frontend.mounted` 佐证）。

### M2-13 pytest smoke —— 通过
- `tests/test_api_risk_real.py + tests/test_0904_c1.py + tests/test_sse_ticket.py`：**21 passed**（含 M2-01~03 三修后的复跑）。
- `ruff check` 三修文件：All checks passed。

### M2-14 npm 构建 —— 未跑（记账说明）
- `npm run build` 未执行：node_modules 已存在但全量构建重型且与本次 src 改动无关；`dist` 已存在故不阻塞结论。
  需要发版前由 E/前端线按 AGENTS §4（`npm run build` + `npm run test`）补齐。

### M2-15 文档与实现不符 2 处 —— 记账（低）
- (a) `docs/recon.md:32` 称 `app=FastAPI` 在 `server.py:322`，实测已在 **`server.py:468`**（D 线演进漂移；issues.md P2-4 已有同类漂移记录）。
  → 记账转 C 线（docs 禁区）。
- (b) AGENTS §3 `npm run dev --port 5173` vs `README.md:76` `npm run dev -- --host --port 5173`：
  npm 语法以后者为准（无 `--` 参数不透传）；实测 `frontend/package.json:7` dev 脚本已自带 `--host 0.0.0.0 --port 5173`，
  故裸 `npm run dev` 即够，两种写法结果恰好一致、无人被坑。→ 记账转 A/C 线统一口径（低）。

---

## §4 M 线修复清单（3 条，各独立 commit，未 push）

| commit | 文件（非碰撞确认） | 内容 | 验证 |
|---|---|---|---|
| `76ebc93` | `src/hero_quant/checkpoint/postgres.py` | M2-01：`_redact_dsn` 对齐 `:pass@` + 收窄 except | 直接调用三形态 + c1 15 passed + ruff |
| `4ad6de1` | `src/hero_quant/api/risk.py` | M2-02：`risk_summary` degraded 加 reason | risk 2 passed + ruff |
| `a27e618` | `src/hero_quant/api/security.py` | M2-03：ttl<=0 死票分支 warning 可观测 | sse_ticket 4 passed + ruff |

- 非碰撞确认：三文件均不在近 8 笔提交的 47 文件碰撞集中（`git log --name-only -8` 逐项核对；三文件亦不在 `migrations/`/禁区）。
- `git add` 均带显式单路径；工作树他人未提交改动（security.py/server.py）未碰、未卷入。

## §5 记账总表（转线）

| 编号 | 定级 | 去向 |
|---|---|---|
| M2-04 卖家归属丢失 | 中 | billing 专线 |
| M2-06残余 共享密钥下绑定票互信 | 中 | D/鉴权重构专线 |
| M2-08 HMAC env 遮蔽显参 | 低 | D 线 |
| M2-09 environ 门禁盲区 | 中 | B 线（门禁在 tests） |
| M2-10 TTL 收敛静默 | 低 | D 线顺手 |
| M2-07 scope 分域观察 | 信息 | D 线（提交者自测） |
| M2-11 G-06/G-07 并发修观察 | 信息 | K 线自带证据 |
| M2-15 文档漂移 ×2 | 低 | C/A 线 |

终态：报告 `docs/redteam-round2.md`；发现 14（修 3 / 记账 8 / 通过·观察 3）；验证 21 passed + ruff clean + /live 200 + /ready 如实 degraded。
