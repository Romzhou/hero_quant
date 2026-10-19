# 安全审计报告（G线）

- 日期：2026-10-19；审计面依据 `docs/recon.md` §4 + `AGENTS.md` §2/§6/§7
- 权限声明：全仓只读审计；`.env` 真值禁读禁贴（未读取）；`migrations/` 只读核对一行未改；
  `src/` 零改动（`src/hero_quant/api/security/` 目录实际不存在，单文件 `api/security.py` 不在授权写范围内，
  且 F 线正在扫荡中，为避碰撞所有高危一律记账转线、不碰代码）
- 方法：静态只读（read/grep/git log -S 存在性核对，未展开 patch 内容、不贴任何疑似密钥值）

## 结论速览

- 高危：3（已修 0 / 记账 3）；中危：5（记账 5）；低危：7（记账 3 / 无需修 4）
- 本报告独占文件 `docs/security-audit.md`（I 线不碰）；src 零改动，无 `G: ...` 修复 commit

---

## 高危（记账转线，不碰）

### G-01 `.env` 明文真密钥残留（recon §4.1-1 转述，G 线未验证值）
- 文件+行号：`docs/recon.md:105`（依据行）；工作区 `.env:3-4`（recon 称存明文真值；G 线按 AGENTS §2 禁读，未打开验证）
- 定级：高
- 状态：记账 → 转 A 线（轮换 + 占位符化；`.env` 从未入库见 G-14，属工作区本地残留风险）
- 备注：git 历史中 `.env` 从未被追踪（`git ls-files` 无命中；`.gitignore:6` 覆盖），`-S HERO_LLM_API_KEY` 命中仅 2 个文档类 commit，
  未展开 patch、无真值入库证据。风险面 = 本地文件 + compose `env_file` 自动加载（`src/hero_quant/config/settings.py:19-34`）进进程/镜像层。

### G-02 票据绑定 user 为调用方自声明，可冒充任意用户订阅 WS 频道
- 文件+行号：
  - 签发：`src/hero_quant/api/server.py:1574`（`user` 查询参自声明）→ `server.py:1611`（`_claimed_user = _norm_uid_ticket(user)`，
    未与 API-Key/HMAC 身份做任何绑定校验）→ `server.py:1629`（`issue_ticket(..., user_id=_claimed_user)`）
  - 消费：`src/hero_quant/api/ws.py:348-355`、`ws.py:406-413`（`_consume_ticket_identity` 取出 ticket_user）
    → `ws.py:54-76`（`_resolve_identity_channel`：ticket_user 非空即派生 `ws:channel:{user}`；匿名票 `""` 时直接信任 `?user=` 声明）
- 定级：高（Broken Access Control：任一持 key 调用方可订阅任意用户频道，读取他人 trace/query 进度流；匿名票 + `?user=` 同样直达）
- 状态：记账 → 转 D 线（`server.py` 唯一归 D 线；修复方向：签发时 user 必须由服务端身份派生或白名单校验，
  匿名票禁止 `?user=` 派生他人频道；`_ticket_user_q` 在 `server.py:1318` 当前被丢弃（`_`），query 面暂无影响，WS 面实际生效）
- 未修原因：`server.py`/`ws.py` 不在 G 线可写范围（仅 `api/security/` 目录可写，而该目录不存在）。

### G-03 billing 真 PG 购买写入 `tenant=buyer`，卖家归属丢失 / 跨租户购买 FK 必败
- 文件+行号：
  - `src/hero_quant/billing/service.py:978`（`_tenant = buyer_tenant`）→ `service.py:983-989`（INSERT 三、四位参数均为 buyer）
    vs SQL `service.py:143-147`（列序 `factor_id, buyer_tenant, tenant, ...`，即 seller 列被填 buyer）
  - 同文件 `service.py:835`（receipt `"tenant": buyer_tenant`，卖家身份全程丢失）
  - 对照 migration 注释 `migrations/002_billing_rls.sql:58`（"seller tenant 由应用显式提供，与 factors 复合 FK 校验一致"）
    与复合 FK `002:28`（`(factor_id, tenant)` 必须与 factors 行一致）
- 定级：高（财务归因完整性：真 PG 下 buyer≠seller 的正常市集购买触发 FK 违例直接抛错（可用性断点）；
  emulated 路径静默落库 buyer 归属行，卖家经 `purchases_seller_select`（`002:56`）永远不可见自己售出的单）
- 状态：记账 → 转 billing 专线（F 线扫荡中，由其认领；修复方向：purchase 解析 factor 的 seller tenant 并写入 `tenant` 列，
  SET LOCAL 仍用 buyer 以满足 `purchases_insert` 的 `buyer_tenant = current_tenant()`）。
- 未修原因：`billing/` 不在 G 线可写范围。

---

## 中危（一律记账不修）

### G-04 补偿删除缺 `SET LOCAL`，FORCE RLS 下补偿静默 no-op 留孤儿行
- 文件+行号：`src/hero_quant/billing/service.py:593-629`（`_pg_delete_factor_sync` 全程无 SET LOCAL）、
  `service.py:1053-1091`（`_pg_delete_purchase_sync` 全程无 SET LOCAL）；
  对照 `migrations/002_billing_rls.sql:37-40`（FORCE RLS）与 `current_tenant()`（`002:3-8`，未设置返回 NULL → `tenant = NULL` 永假）
- 定级：中（仅真 PG + ledger 失败复合条件下触发；触发后计费行已提交、ledger 无记录，恰是补偿机制声称要消灭的半提交）
- 状态：记账 → 转 billing 专线（修复方向：补偿删除建连接后先执行双键 SET LOCAL，与 `_pg_insert_purchase_sync:995-1009` 对齐）

### G-05 service 层 DSN 回退绕过 settings 共享库 opt-in 门
- 文件+行号：`src/hero_quant/billing/service.py:215-222`（`_billing_dsn_from_env` 无声回退 `HERO_PG_DSN`/`HERO_CHECKPOINT_DSN`）
  vs `src/hero_quant/config/settings.py:203-230`（必须 `HERO_BILLING_ALLOW_SHARED_DB=1` 显式 opt-in，否则 fail-closed 返回 None，
  注释明示"财务数据与 checkpoint 混库有表冲突/资金完整性风险"）
- 定级：中（走 service 直连的调用方在未 opt-in 时仍混库；settings 门禁被旁路）
- 状态：记账 → 转 billing 专线（修复方向：service 回退同样要求 opt-in 环境位，或删除回退只认 `HERO_BILLING_DSN`）

### G-06 票据签发限流 fail-open（异常即放行签发）
- 文件+行号：`src/hero_quant/api/server.py:1582-1594`
  （`except Exception: logger.debug("ticket.ratelimit_failed ...")` 后继续向下签发；
  对照同文件 `_check_rate_limit:503-538` 的 fail-closed 503 语义 decoration 不一致；
  另限流键 `server.py:1591` `f"ticket:{client_ip}"` 缺 `hero:` 前缀，与 `infra/redis.py:26` 命名空间惯例分叉）
- 定级：中（Redis 故障/异常时票据无限签发，放大 G-02 冒充面；debug 级日志难被发现）
- 状态：记账 → 转 D 线（修复方向：异常分支 503 fail-closed + 日志提 warning + 键加 `hero:ratelimit:` 前缀）

### G-07 限流键无租户维度（跨租户公平性缺口）
- 文件+行号：`src/hero_quant/api/server.py:503-538`（key=`{endpoint}:{ip}`）、
  `src/hero_quant/api/rate_limiter.py:34-50`（key=`user:{id}`/`ip:{ip}`，无 tenant 参与）
- 定级：中（同一出口 IP/NAT 后多租户共享桶；`ip:unknown` 已收敛小桶 `rate_limiter.py:23` + `server.py:509-521` 缓解误伤，
  但反向"单租户打满拖累同 IP 他人"仍存在）
- 状态：记账 → 转 D 线（修复方向：key 混入 tenant（有身份时），或按 tenant 维度配额；属配额模型变更，需 D 线评估）

### G-08 票据无 audience/scope 绑定，query 票可用于 /metrics
- 文件+行号：`src/hero_quant/api/server.py:1279-1299`（`/metrics` 接受任意有效 `consume_ticket_with_user` 票据，
  注释称"复用 query 的 X-Ticket 票据逻辑"）；签发端 `src/hero_quant/api/security.py:153-195` 无 scope/audience 字段
- 定级：中（单次消费限制了重放，但端点间权限未隔离；/metrics 鉴权强度被拉低到"任一 query 票"）
- 状态：记账 → 转 D 线（修复方向：ticket payload 加 `scope`（query/metrics/stream），消费端校验；属契约变更需同步单测）

---

## 低危

### G-09 `server._redact_dsn_api` 漏空用户名 `:pass@` 形态
- 文件+行号：`src/hero_quant/api/server.py:879-883`（正则 `://([^:]+):[^@]*@` 要求用户名非空）
  vs `src/hero_quant/config/settings.py:37-45`（已覆盖两种形态）
- 定级：低（触发需空用户名 DSN + 走 server 脱敏路径打日志；settings 主路径已安全）
- 状态：记账 → 转 D 线（修复方向：与 settings 共用同一 `_redact_dsn` 实现，消灭分叉）

### G-10 `_real_pg_publish_sync` 重复定义，首个为死代码
- 文件+行号：`src/hero_quant/billing/service.py:283-298`（死桩：仅 `__hashed__` 告警返 False 即被覆盖）
  vs `service.py:496-567`（真正实现，SET LOCAL 双写 + commit）
- 定级：低（当前行为以后定义为准，无功能影响；但"publish 降级语义" зависимости看第 283 行会误判）
- 状态：记账 → 转 billing 专线（删死定义，保留 `_pg_publish_sync:569` 统一入口注释）

### G-11 `HERO_ALLOW_INSECURE` 历史兼容开关
- 文件+行号：`src/hero_quant/api/security.py:469-490`（`HERO_ALLOW_INSECURE=1` + 非 development/非 pytest 即阻断并告警；
  `HERO_ALLOW_INSECURE_DEV` 另要求 `HERO_ENV=development` + 回环 IP）
- 定级：低（生产默认 fail-closed：`security.py:491-503` 无 key 即拒绝；开关需多条件同时满足才放行）
- 状态：无需修（记录在案；建议后续版本删除该兼容分支，届时由 D 线评估单测影响）

### G-12 回测 bundle 缓存键无租户/用户维度
- 文件+行号：`src/hero_quant/api/server.py:2015-2019`
 （`_backtest_bundle_cache_key()` 参数固定 `{"v":1,...}`，键 `hero:cache:backtest:bundle:{hash}` 全局共享）
- 定级：低（当前 bundle 为全局合成 demo 数据，无租户私密成分；见 `server.py:2007` 注释）
- 状态：无需修（前置条件：未来参数/数据按租户分化时，必须把 tenant 混入 hash，否则跨租户串扰；届时由 D 线修）

### G-13 session 模块暂无生产调用点， fixation 面休眠
- 文件+行号：`src/hero_quant/infra/session.py:31-36`（空 id 抛错禁收敛共享键，好）、
  `session.py:39-94`（set/get 实现）；全仓 grep `set_session(`/`get_session(` 在 `src/` 内无调用方
- 定级：低（键隔离 `hero:session:` + 归一化 + 128 上限均到位；风险仅在启用时：若 session_id 由客户端供给即 fixation）
- 状态：无需修（启用前置条件：服务端生成随机 id + 绑定身份，届时由接入线修）

### G-14 git 历史无密钥入库证据；`.env` 从未被追踪
- 文件+行号：`.gitignore:6`（`.env` 忽略）；`git ls-files` 无 `.env` 命中；
  `git log -S HERO_LLM_API_KEY` 仅命中 2 个文档类 commit（未展开 patch，未贴值）；
  `git log --all -- .env` 零命中；`git log -S "sk-"` 命中为脱敏/红线修复类 commit（同上未展内容）
- 定级：低（历史面干净；残余风险仅工作区本地 `.env`，已由 G-01 覆盖）
- 状态：无需修

### G-15 已到位的纵深（肯定项，无需修）
- 票据原子消费：`src/hero_quant/api/security.py:210-234`、`237-271`（GETDEL 优先 → Lua 兜底，无 GET-then-DEL 裸回退；TTL 有界 `153-175`）
- Host 白名单 fail-closed：`security.py:311-326`（空表拒绝；IPv6/端口规范化 `284-308`）；仅认 `headers.host`（`446-460`）
- HMAC/API-Key 常量时间比较：`security.py:415`、`435`、`515`；缺 key 缺 secret 一律 False（`355-361`、`491-503`）
- Billing RLS DDL 本体：`migrations/002_billing_rls.sql:36-66`（FORCE + 买卖家分 policy + 复合 FK `002:28` + 索引 `002:31-34`）；
  应用 DDL 内联与 migration 一致（`service.py:108-141`）
- DSN 键哈希化：`service.py:173-183`（`_dsn_key` sha256，口令不驻留 dict key）；DSN 日志只记脱敏 host（`server.py:109-163`）
- 脱敏瀑布 fail-closed：`src/hero_quant/security/redaction.py:106-108`（未知 sink 回退最严）、`216-227`（异常 raise 前不泄明文）

---

## 未修原因总述

- G 线可写范围仅 `src/hero_quant/api/security/` 目录——该目录在当前树中不存在（鉴权实现实为单文件
  `src/hero_quant/api/security.py` + `src/hero_quant/security/` 包 + `server.py`/`ws.py` 内联逻辑）。
- 按任务指令"高危在别处→记账转对应线，不碰；中危一律记账不修"，本轮 src 零改动，无 `G: ...` 修复 commit。
- 唯一写操作为本报告 `docs/security-audit.md`（G 线独占，I 线不碰）。

## 转线清单

| 编号 | 转往 | 文件 |
|---|---|---|
| G-01 | A 线 | 工作区 `.env` 轮换 + 占位符化 |
| G-02 / G-06 / G-07 / G-08 / G-09 | D 线 | `server.py`（D 线独占）+ `ws.py` 身份通道语义 |
| G-03 / G-04 / G-05 / G-10 | billing 专线（F 线扫荡中，由其认领或单独立专线） | `billing/service.py` |
| G-12（未来） | D 线 | `server.py:2015-2019` |
| G-13（未来） | 接入线 | `infra/session.py` 启用时 |
