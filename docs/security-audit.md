# 安全审计报告（G线）

- 日期：2026-10-19；审计面依据 `docs/recon.md` §4 + `AGENTS.md` §2/§6/§7
- 权限声明：全仓只读审计；`.env` 真值禁读禁贴（未读取）；`migrations/` 只读核对一行未改；
  `src/` 零改动（`src/hero_quant/api/security/` 目录实际不存在，单文件 `api/security.py` 不在授权写范围内，
  且 F 线正在扫荡中，为避碰撞所有高危一律记账转线、不碰代码）
- 方法：静态只读（read/grep/git log -S 存在性核对，未展开 patch 内容、不贴任何疑似密钥值）

## 结论速览

- 高危：3（已修 0 / 记账 3，其中 G-03 待 billing 架构专线不动）；中危：5（已修 5）；低危：7（已修 7，其中 G-11~G-15 为加固/回归锁）
- 本报告独占文件 `docs/security-audit.md`（I 线不碰）；K 线 12 条修复 commit（`K: ...` 前缀，见各条状态行），G-03 不动

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
- 状态：已修（`e2708a2`）→ K 线：`_pg_delete_factor_sync` 新增可选 tenant 参（调用方传入归属租户）+ `_set_local_dual_tenant` 双写 `app.tenant`/`app.current_tenant`，`_pg_delete_purchase_sync` 以 buyer 双写后删；与 `_pg_insert_purchase_sync` 对齐，FORCE RLS 下不再静默 no-op

### G-05 service 层 DSN 回退绕过 settings 共享库 opt-in 门
- 文件+行号：`src/hero_quant/billing/service.py:215-222`（`_billing_dsn_from_env` 无声回退 `HERO_PG_DSN`/`HERO_CHECKPOINT_DSN`）
  vs `src/hero_quant/config/settings.py:203-230`（必须 `HERO_BILLING_ALLOW_SHARED_DB=1` 显式 opt-in，否则 fail-closed 返回 None，
  注释明示"财务数据与 checkpoint 混库有表冲突/资金完整性风险"）
- 定级：中（走 service 直连的调用方在未 opt-in 时仍混库；settings 门禁被旁路）
- 状态：已修（`953fb5c`）→ K 线：service `_billing_dsn_from_env` 回退 `HERO_PG_DSN`/`HERO_CHECKPOINT_DSN` 同 settings 门禁，须 `HERO_BILLING_ALLOW_SHARED_DB=1/true/yes/on` 显式 opt-in，否则 fail-closed 返回 None（未 opt-in 回退记 warning，不静默混库）；`HERO_BILLING_DSN`/显式参数直认不变

### G-06 票据签发限流 fail-open（异常即放行签发）
- 文件+行号：`src/hero_quant/api/server.py:1582-1594`
  （`except Exception: logger.debug("ticket.ratelimit_failed ...")` 后继续向下签发；
  对照同文件 `_check_rate_limit:503-538` 的 fail-closed 503 语义 decoration 不一致；
  另限流键 `server.py:1591` `f"ticket:{client_ip}"` 缺 `hero:` 前缀，与 `infra/redis.py:26` 命名空间惯例分叉）
- 定级：中（Redis 故障/异常时票据无限签发，放大 G-02 冒充面；debug 级日志难被发现）
- 状态：已修（`67d27d5`）→ K 线：票据签发限流异常分支 503 fail-closed（`_inc_api_error(ticket, ratelimit_unavailable)` + warning + `ticket.ratelimit_failed_503`，异常类收窄与 `_check_rate_limit` 同口径）；键 `ticket:{ip}` 经 infra RateLimiter 自动落 `hero:ratelimit:` 前缀（`infra/redis.py:53`），与命名空间惯例一致，无分叉

### G-07 限流键无租户维度（跨租户公平性缺口）
- 文件+行号：`src/hero_quant/api/server.py:503-538`（key=`{endpoint}:{ip}`）、
  `src/hero_quant/api/rate_limiter.py:34-50`（key=`user:{id}`/`ip:{ip}`，无 tenant 参与）
- 定级：中（同一出口 IP/NAT 后多租户共享桶；`ip:unknown` 已收敛小桶 `rate_limiter.py:23` + `server.py:509-521` 缓解误伤，
  但反向"单租户打满拖累同 IP 他人"仍存在）
- 状态：已修（`b7c697a`）→ K 线：`rate_limiter.limit_key` 新增 `_extract_tenant`（`request.state.current_tenant/tenant` 或 `user.tenant`），有身份时键为 `user:{id}:tenant:{t}`/`ip:{ip}:tenant:{t}`；`server._check_rate_limit` 同理混入 tenant 后缀；无身份回退 legacy 键（`test_0904_c4` 绿，配额模型向后兼容）

### G-08 票据无 audience/scope 绑定，query 票可用于 /metrics
- 文件+行号：`src/hero_quant/api/server.py:1279-1299`（`/metrics` 接受任意有效 `consume_ticket_with_user` 票据，
  注释称"复用 query 的 X-Ticket 票据逻辑"）；签发端 `src/hero_quant/api/security.py:153-195` 无 scope/audience 字段
- 定级：中（单次消费限制了重放，但端点间权限未隔离；/metrics 鉴权强度被拉低到"任一 query 票"）
- 状态：已修（`f68052f`）→ K 线：ticket payload 加 `scope`（query/metrics/stream，`security._TICKET_SCOPES` 白名单；`issue_ticket(..., scope)`，None=legacy 无分域）；`consume_ticket[_with_user]` 新增 `require_scope`（legacy 迁移期放行，不匹配 fail-closed 销毀 + warning）；`/metrics` 要求 metrics、`/v1/query` 要求 query、stream 接受 query/stream、签发端缺省 query（`test_api/test_sse_ticket` 绿）

---

## 低危

### G-09 `server._redact_dsn_api` 漏空用户名 `:pass@` 形态
- 文件+行号：`src/hero_quant/api/server.py:879-883`（正则 `://([^:]+):[^@]*@` 要求用户名非空）
  vs `src/hero_quant/config/settings.py:37-45`（已覆盖两种形态）
- 定级：低（触发需空用户名 DSN + 走 server 脱敏路径打日志；settings 主路径已安全）
- 状态：已修（`cbe699c`）→ K 线：`server._redact_dsn_api` 改为薄委托 `settings._redact_dsn`（正典），分叉消除，`:pass@` 空用户名形态同步覆盖

### G-10 `_real_pg_publish_sync` 重复定义，首个为死代码
- 文件+行号：`src/hero_quant/billing/service.py:283-298`（死桩：仅 `__hashed__` 告警返 False 即被覆盖）
  vs `service.py:496-567`（真正实现，SET LOCAL 双写 + commit）
- 定级：低（当前行为以后定义为准，无功能影响；但"publish 降级语义" зависимости看第 283 行会误判）
- 状态：已修（`d7f8c1b`）→ K 线：删首个死定义（`service.py:283-298` 旧桩），保留真实现 + `_pg_publish_sync` 统一入口注释；行为以后定义为准，零行为变化

### G-11 `HERO_ALLOW_INSECURE` 历史兼容开关
- 文件+行号：`src/hero_quant/api/security.py:469-490`（`HERO_ALLOW_INSECURE=1` + 非 development/非 pytest 即阻断并告警；
  `HERO_ALLOW_INSECURE_DEV` 另要求 `HERO_ENV=development` + 回环 IP）
- 定级：低（生产默认 fail-closed：`security.py:491-503` 无 key 即拒绝；开关需多条件同时满足才放行）
- 状态：已修（加固，`abd51df`）→ K 线：`HERO_ALLOW_INSECURE=1` 命中即 `DeprecationWarning` + warning 可观测（`security.api_key_allow_insecure_legacy_deprecated`），fail-closed 语义不变（非 development 且非 pytest 仍阻断；`test_api_security_real` 绿）；删除该分支待后续版本由 D 线评估单测影响

### G-12 回测 bundle 缓存键无租户/用户维度
- 文件+行号：`src/hero_quant/api/server.py:2015-2019`
 （`_backtest_bundle_cache_key()` 参数固定 `{"v":1,...}`，键 `hero:cache:backtest:bundle:{hash}` 全局共享）
- 定级：低（当前 bundle 为全局合成 demo 数据，无租户私密成分；见 `server.py:2007` 注释）
- 状态：已修（加固，`c8c6487`）→ K 线：`_backtest_bundle_cache_key(tenant=None)` 可选混入 tenant（非空归一后参与 hash）；`_read/_write_backtest_bundle_cache` 透传；缺省 None 时键与旧全局键完全一致（当前 bundle 仍全局合成 demo 数据，无串扰）

### G-13 session 模块暂无生产调用点， fixation 面休眠
- 文件+行号：`src/hero_quant/infra/session.py:31-36`（空 id 抛错禁收敛共享键，好）、
  `session.py:39-94`（set/get 实现）；全仓 grep `set_session(`/`get_session(` 在 `src/` 内无调用方
- 定级：低（键隔离 `hero:session:` + 归一化 + 128 上限均到位；风险仅在启用时：若 session_id 由客户端供给即 fixation）
- 状态：已修（加固，`85bf635`）→ K 线：新增 `generate_session_id()`（`secrets.token_urlsafe(32)`，失败不回退可预测值）+ `create_session(data, owner)`（建会话唯一入口，owner 写入 `_owner` 保留键）+ `check_session_owner()`（常量时间比较）；既有 `set/get` 语义不变（`test_0904_a4` 绿）；调用方须用服务端 id，禁客户端供给 id 建会话

### G-14 git 历史无密钥入库证据；`.env` 从未被追踪
- 文件+行号：`.gitignore:6`（`.env` 忽略）；`git ls-files` 无 `.env` 命中；
  `git log -S HERO_LLM_API_KEY` 仅命中 2 个文档类 commit（未展开 patch，未贴值）；
  `git log --all -- .env` 零命中；`git log -S "sk-"` 命中为脱敏/红线修复类 commit（同上未展内容）
- 定级：低（历史面干净；残余风险仅工作区本地 `.env`，已由 G-01 覆盖）
- 状态：已修（回归锁，`293c511`）→ K 线：新增 `tests/test_k_g14_env_guard.py`（`.gitignore` 覆盖 `.env`、正典脱敏覆盖两种 userinfo 形态、server 与正典一致；全程不读 `.env` 真值）；git 历史面结论不变（`.env` 从未追踪，见原备注）

### G-15 已到位的纵深（肯定项，回归锁 `4dbb35b`）
- 状态：已修（回归锁，`4dbb35b`）→ K 线：新增 `tests/test_k_g15_defense_lock.py` 锁定 6 项（票据原子单次 + scope 隔离运行时、Host fail-closed、compare_digest 存在性、billing RLS DDL 与 migration 只读一致、DSN 键哈希化、未知 sink 回退最严）；src 零改动
- 票据原子消费：`src/hero_quant/api/security.py:210-234`、`237-271`（GETDEL 优先 → Lua 兜底，无 GET-then-DEL 裸回退；TTL 有界 `153-175`）
- Host 白名单 fail-closed：`security.py:311-326`（空表拒绝；IPv6/端口规范化 `284-308`）；仅认 `headers.host`（`446-460`）
- HMAC/API-Key 常量时间比较：`security.py:415`、`435`、`515`；缺 key 缺 secret 一律 False（`355-361`、`491-503`）
- Billing RLS DDL 本体：`migrations/002_billing_rls.sql:36-66`（FORCE + 买卖家分 policy + 复合 FK `002:28` + 索引 `002:31-34`）；
  应用 DDL 内联与 migration 一致（`service.py:108-141`）
- DSN 键哈希化：`service.py:173-183`（`_dsn_key` sha256，口令不驻留 dict key）；DSN 日志只记脱敏 host（`server.py:109-163`）
- 脱敏瀑布 fail-closed：`src/hero_quant/security/redaction.py:106-108`（未知 sink 回退最严）、`216-227`（异常 raise 前不泄明文）

---

## 未修原因总述（K 线清账后）

- K 线授权覆盖 `billing/`、`rate_limiter`、`ticket/scope`、`redis` 键、`session`、脱敏、`publish` 去重、
  `INSECURE` 开关、`backtest` 缓存键 + 独占报告文件；改前均 `git log --oneline -5` 核对，
  并行 M/L 线提交（checkpoint 脱敏、issue_ticket warning、e2e）经读 diff 确认兼容后下手，无硬上。
- G-03（billing tenant 归属）按红线不动，待 billing 架构专线；G-01（工作区 `.env` 真值）转 A 线轮换；
  G-02（票据 user 绑定）已由 D/F 收敛发侧+订阅侧，K 线 G-08 在其上叠加 scope 分域，未改其语义。
- `migrations/` 全程只读（G-15 锁中仅读 `002` 核对一行，未改）；`.env` 真值禁读（全程未打开）；
  billing 改动 fail-closed 语义未弱化（无 pool 返 False/None 路径原样保留）。
- 自验：`ruff check` 所改 7 文件全过；`test_0904_c4` 必跑 + 票据/鉴权/计费/会话/回测子集共 250 项绿（见终态）。

## 转线清单（K 线清账后）

| 编号 | 去向 | 文件 |
|---|---|---|
| G-01 | A 线（未动） | 工作区 `.env` 轮换 + 占位符化 |
| G-02 | D/F 已收敛（K 未改其语义，G-08 叠加 scope） | `server.py` + `ws.py` 身份通道语义 |
| G-03 | billing 架构专线（K 红线未动） | `billing/service.py:978,835` + `002:28,58` |
| G-04 / G-05 / G-10 | K 已修（`e2708a2` / `953fb5c` / `d7f8c1b`） | `billing/service.py` |
| G-06 / G-07 / G-08 / G-09 / G-12 | K 已修（`67d27d5` / `b7c697a` / `f68052f` / `cbe699c` / `c8c6487`） | `server.py` + `rate_limiter.py` + `security.py` |
| G-11 | K 加固（`abd51df`，删除待后续版本） | `security.py:469-490` |
| G-13 | K 加固（`85bf635`） | `infra/session.py` |
| G-14 / G-15 | K 回归锁（`293c511` / `4dbb35b`） | `tests/test_k_g14|g15_*.py` |
