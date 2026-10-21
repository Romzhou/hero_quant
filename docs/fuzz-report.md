# O线 API 模糊测试报告（fake/offline LLM 模式）

- 日期：2026-10-19 · 专线：O（API 模糊测试）· 服务：`uvicorn hero_quant.api.server:app --port 8899`（`src/hero_quant/api/server.py:322`）
- OpenAPI：`/openapi.json`（13 paths；`/v1/*` 共 9 个 operation）· fake 模式：`langchain_openai` 未安装 → `LLMFactory.create` 走 `_FallbackChat` 离线回显（见 O1）
- 写权限遵守：仅新建 `docs/fuzz-report.md` + `scripts/fuzz_repro.py` + `scripts/st_run.py` + `tests/fuzz/test_o_fuzz_edges.py`；`src/` 零改动；`migrations/` 未碰；未读 `.env` 真值

## 1. 请求数与 500 统计

| 轮次 | 方法 | 请求数 | 500/未捕获异常 | 备注 |
|---|---|---|---|---|
| schemathesis 冒烟 | OpenAPI property（6 ops：backtest×4、trace×1、risk×2），`--checks not_a_server_error` | 79 | 0 | `schemathesis-smoke.json` exit 0 |
| schemathesis 放大 | 同上 + `POST /v1/query/ticket`，`-n 50` | 177 | 0 | `schemathesis-full.json` exit 0 |
| 定向 F1 ticket 非法 scope | 非法枚举 10 种 | 10 | 0 | 9×400 + 1×200（`query␣` 归一化，见 O3） |
| 定向 F2 ticket user | 超长/XSS/SQLi/unicode/穿越 7 种 | 7 | 0 | 全部 200（匿名票降级绑定，G-02 符合预期） |
| 定向 F3 方法/类型混淆 | GET/PUT/DELETE + scope 数组 | 4 | 0 | GET→404（见 O2），其余 405 |
| 定向 F4 票据鉴权 | 无票/伪票/超长票/单次复用 | 5 | 0 | 401/401/401/200/401，单次语义正确 |
| 定向 F5 q 超长/特殊 | 空/20KB/200KB/unicode/SQLi/穿越/换行 | 7 | 0 | 200KB→400 系 HTTP 解析层拒绝（见 O5） |
| 定向 F6 参数类型混淆 | use_graph 非 bool×2、wall_time 非法×3、穿越×4、数组/未知参数 | 12 | 0 | 422/400/200 组合，无 500（含 NaN/负数/1e9，见 O6） |
| 定向 F7 stream 票据面 | 无票/错分域/超长 q | 3 | 0 | 403/403/200 |
| 定向 F8 trace offset | 负数/超大/非数字/0x/空串/数组 | 9 | 0 | 400（负数）/422（类型）/200（其余） |
| 定向 F9 方法混淆+risk/metrics | POST/PUT/DELETE 打 GET 路由×5、risk、metrics | 7 | 0 | 405×5；risk 200+degraded+reason；metrics 无票 401（预期，T4-1/G-08） |
| 零散探针 | 起服验证/复现确认 | ~10 | 0 | 含 F5-2 400 复现、degraded 体确认 |
| **合计 live** | | **≈330** | **0** | **500 归零：无 500，无未捕获异常冒泡** |
| pytest 锁定 | `tests/fuzz/test_o_fuzz_edges.py`（进程内 TestClient） | 9 用例 | 0 失败 | 见 §4 |

## 2. 500 列表

无。两轮 schemathesis（256 cases，`not_a_server_error`）+ 64 定向 live 请求 + 9 pytest，**500 = 0**。

## 3. 复现路径（每类一条 curl，X-Ticket 须现取现用，单次有效）

```bash
# F1 非法 scope → 400（fail-closed）
curl -s -o /dev/null -w '%{http_code}' -X POST 'http://127.0.0.1:8899/v1/query/ticket?scope=admin'            # 400
# F1 空格归一化 → 200（O3）
curl -s -X POST 'http://127.0.0.1:8899/v1/query/ticket?scope=query%20' | head -c 120                          # 200
# F2 超长 user → 200（匿名降级绑定，G-02）
python -c "print('x'*5000)" | xargs -0 -I{} curl -s -o /dev/null -w '%{http_code}' -X POST --get --data-urlencode 'user={}' 'http://127.0.0.1:8899/v1/query/ticket'
# F3 GET 打 POST 路由 → 404（O2）；PUT/DELETE → 405
curl -s -o /dev/null -w '%{http_code}' 'http://127.0.0.1:8899/v1/query/ticket'                                # 404
curl -s -o /dev/null -w '%{http_code}' -X PUT 'http://127.0.0.1:8899/v1/query/ticket'                          # 405
# F4 无票/伪票/复用
curl -s -o /dev/null -w '%{http_code}' 'http://127.0.0.1:8899/v1/query?q=hi'                                  # 401
T=$(curl -s -X POST 'http://127.0.0.1:8899/v1/query/ticket' | python -c "import sys,json;print(json.load(sys.stdin)['ticket'])")
curl -s -o /dev/null -w '%{http_code}' -H "X-Ticket: $T" 'http://127.0.0.1:8899/v1/query?q=hi'                # 200
curl -s -o /dev/null -w '%{http_code}' -H "X-Ticket: $T" 'http://127.0.0.1:8899/v1/query?q=hi'                # 401（已消费）
# F5 200KB q → 400（HTTP 解析层，O5）
python -c "import urllib.parse;print(urllib.parse.urlencode({'q':'B'*200000}))" > /tmp/bigq.txt  # 体积约 200KB
curl -s -w '%{http_code}' -H "X-Ticket: $T2" "http://127.0.0.1:8899/v1/query?$(cat /tmp/bigq.txt)" | tail -c 60
# F6 穿越 → 400；类型错误 → 422
curl -s -o /dev/null -w '%{http_code}' -H "X-Ticket: $T3" 'http://127.0.0.1:8899/v1/query?q=x&replay_path=..%2F..%2Fetc%2Fpasswd'  # 400
curl -s -o /dev/null -w '%{http_code}' -H "X-Ticket: $T4" 'http://127.0.0.1:8899/v1/query?q=x&wall_time_budget=abc'                # 422
# F8 offset
curl -s -o /dev/null -w '%{http_code}' 'http://127.0.0.1:8899/v1/trace/events?offset=-1'                       # 400
curl -s -o /dev/null -w '%{http_code}' 'http://127.0.0.1:8899/v1/trace/events?offset=abc'                      # 422
# F9 risk 降级带因
curl -s 'http://127.0.0.1:8899/v1/risk/summary' | head -c 300   # 200 + degraded:true + reason
```

## 4. 根因分类与处理（O1/O6 已修，O2 文档确认，O3/O4/O5 Round 6 波次 2 收口；仅 O1/O6 触碰 src）

- **500/未捕获异常：0 条，无根因。**
- O1（degraded 标记不全，链A/D线归属）：**已修**。两处 `_FakeLLM` 模板以 `src/hero_quant/api/server.py:1444`（query 分支）为准统一 `src/hero_quant/api/server.py:1840`（stream 分支）：模板嵌入 `for query:{goal}`、尾部追加固定免责声明行 `本结果为合成占位，不构成投资建议`，并删除长模板多出的 3 行（数据来源/回测区间/等权策略跑赢基准）。body 侧 `degraded:true` + 非空 `degraded_reason` + `llm_mode=synthetic`（`src/hero_quant/api/server.py:1575-1604`），经 `_with_degraded_headers`（`src/hero_quant/api/server.py:184-192`）同步响应头 `X-Degraded/X-Degrade-Reason`；stream 首事件对应（`src/hero_quant/api/server.py:1975-1984` + 响应头 `src/hero_quant/api/server.py:2084-2089`）。`evals/run.py:39` 的 `FAKE_CHECKABLE` 加 `risk_warning` marker 与 FakeLLM 模板（`evals/run.py:52-55`）同步。回归锁：`tests/fuzz/test_o_wall_time.py:84-110`（源码级模板一致断言 + evals 对拍）。
- O2（GET /v1/query/ticket → 404，余者 405）：**不修关票**。唯一 POST 路由 `src/hero_quant/api/server.py:1614`；GET 落入 SPA catch-all（`src/hero_quant/api/server.py:2586-2594` / `src/hero_quant/api/server.py:2646-2651`）的 `v1/` 守卫（`src/hero_quant/api/server.py:2593-2594` / `src/hero_quant/api/server.py:2650-2651`）返回 404，fail-closed；PUT/DELETE 无路由命中由框架默认 405。404 与 405 均拒绝非 POST 方法，等价安全，无 500。锁定测试：`tests/fuzz/test_o_fuzz_edges.py:31-36`。
- O3（`scope="query "` → 200）：**已收口（Round 6 波次 2 / W2 线，2026-10-21）——判定「有意设计，非越权」→ 写锁测试，零 src 改动**。依据：签发端唯一入口 `src/hero_quant/api/server.py:1670-1674` 先 `_normalize_scope(scope)`（归一化失败 → 400 fail-closed），写进票据载荷的是**归一化后的值**（`:1710` → `src/hero_quant/api/security.py:253-254` 二次归一化，同一白名单）；消费端 `src/hero_quant/api/security.py:111-136` `_parse_ticket_scope` 只认载荷里的**精确**分域值、`:139-151` `_scope_allowed` 做分域比对 ⇒ **原始 scope 串从不流入下游**，空白归一化是"单一入口的一致规范化"，不存在"原始串绕过白名单"的越权面；归一化后仍是**恰好 query 分域**（query 面可消费、metrics 面 fail-closed 拒绝），未放大成 legacy 全域票。选 A（锁测试）不选 B（改 src 收紧）的理由：① 归一化不放大权限（metrics 面仍拒）；② 改 src 会推翻既有锁 `tests/fuzz/test_o_fuzz_edges.py:25-28` 与 §3 复现路径（`scope=query%20` → 200），属推翻已冻结合同、超出"收口悬案"范围；③ 非法 scope（`queryx`/空串/纯空白/大写）仍 400 fail-closed，收紧收益为零、回归面为负。回归锁：`tests/fuzz/test_o3_scope_normalization.py`（5 用例）。**复核修正（与首轮记述不同）**：伪造载荷塞原始 `"query "` 消费时**不是**"fail-closed 拒绝"，而是 `_parse_ticket_scope` 不识别该串 → 按 G-08 legacy 兼容降级为"无分域票"（与"载荷里完全没有 scope"完全等价，迁移期放行）；原始串不会以 query 身份流向下游，伪造前提是已能写入票据存储（Redis 属可信基础设施边界）⇒ 本次不修，记 §5 遗留台账。
- O4（HERO_ENV 未设 + 无 Redis → ticket 全 429）：**已收口（Round 6 波次 2 / W2 线，2026-10-21）——判定「设计意图，符合 G-06 fail-closed 精神」→ 写锁测试，零 src 改动**。依据：`src/hero_quant/infra/redis.py:761-803` `RateLimiter.try_acquire_sync` 无后端（`get_redis_sync()` → None）→ **直接 False**（`:764-767` fail-closed 不放行）；`src/hero_quant/api/server.py:1640-1641` → 429 且**不签票**，`:1642-1651` 限流器异常 → 503 `Rate limiter unavailable`。生产门禁 `src/hero_quant/infra/redis.py:220-237` `_require_real_redis_allowed()`：fakeredis 回退**仅**在 `HERO_ENV=development` 或 `PYTEST_CURRENT_TEST` 存在时允许，其余 fail-closed（返回 None → 限流/票据随即拒绝）。**陷阱（已落锁）**：pytest 进程恒带 `PYTEST_CURRENT_TEST` ⇒ 回退在 pytest 内**永远**允许 ⇒ "Redis 不可达 → 429" 不能靠改 env 触发，锁测试改为**主动注入**不可用（monkeypatch `hero_quant.infra.redis.get_redis_sync`：无后端 → 429、后端抛错 → 503），门禁函数本身另单独锁定（`HERO_ENV=development` → True；无 `HERO_ENV` 且无 `PYTEST_CURRENT_TEST` → False）。分层语义：入口 429 来自限流层无后端（防无保护超发），底层 `src/hero_quant/api/security.py:262-280` `issue_ticket` 在 Redis 不可用时降级进程内存储（单次消费语义保持）——两者不矛盾。回归锁：`tests/fuzz/test_o4_redis_failclosed.py`（5 用例）。
- O5（200KB q → 400 `Invalid HTTP request received`）：**已收口（Round 6 波次 2 / W2 线，2026-10-21）——框架层拒绝，非应用缺陷，零代码改动**。定位：200KB 的 `q` 被 **uvicorn/httptools 的 HTTP 解析层**（请求行/URL 长度上限）拒绝，返回 400 `Invalid HTTP request received`；请求**根本没进 FastAPI**（应用层无异常、无 500、无应用日志冒泡，仅框架 access log 记 400）⇒ 应用层无从修复，且这不是 AGENTS §6 的 fail-closed 缺口（是**拒绝**而非假成功/假活）。若将来要支持超大 query，应在**网关/代理层**放开（nginx `large_client_header_buffers` / `client_header_buffer_size`），**不在应用层改**；应用层已有业务参数边界校验（`wall_time_budget`，见 O6）。
- O6（`wall_time_budget=NaN/-5/1e9` → 200）：**已修 422**。API 边界为 `0 < x <= 3600`：`NaN/-5/0/3600.1/inf/-inf/1e9` → 422，合法 `1/3600` → 200。校验函数 `_validate_wall_time_budget`（`src/hero_quant/api/server.py:1321-1337`）置于限流（`src/hero_quant/api/server.py:1351` / `src/hero_quant/api/server.py:1721`）之后、ticket 消费（`src/hero_quant/api/server.py:1359` / stream 侧同序）之前：非法值直接 422 且不耗单次票（同一票连打非法值仍全 422、票仍可正常消费一次），无票 + 非法值时优先 422。只校验显式传入值（`None` 放行走 settings fallback：`src/hero_quant/api/server.py:1512-1514` / `src/hero_quant/api/server.py:1906-1908`，fallback 不钳制、不改默认）。`NaN` 须 `math.isnan` 显式拦截（比较符抓不住：`NaN > 0` 为 False 会落入旧归一化分支静默变 `None`，`src/hero_quant/agent/loop.py:365`；`inf` 同理经 `math.isinf` 拦截）。env 正数门保持：`src/hero_quant/config/settings.py:48-58`。回归锁：`tests/fuzz/test_o_wall_time.py:31-82`（query/stream 双端非法 422 + 合法 200 + 不耗票证据）。
- 工具链注记：schemathesis 4.32.1 CLI 在 Windows gbk 控制台必崩（rich `✅` 编码，`_win32_console.py:402`），与被测服务无关；O 线用 `scripts/st_run.py`（`is_terminal=False` monkeypatch） headless 运行，256 cases 全过。

## 5. 修复/记账清单

- src 修复：**有**——O1（`src/hero_quant/api/server.py:1444` + `src/hero_quant/api/server.py:1840` 两模板统一 + `evals/run.py:39,52-55` 同步）与 O6（`src/hero_quant/api/server.py:1321-1337` 校验器 + 两处调用点 `src/hero_quant/api/server.py:1354-1355` / `src/hero_quant/api/server.py:1724-1725`），另 `src/hero_quant/api/server.py:15` 增 `import math`（NaN/inf 拦截用）。
- 新增文件：`docs/fuzz-report.md`（本报告）、`scripts/fuzz_repro.py`（64 用例定向 fuzz，`--only F1..F9` 可选跑）、`scripts/st_run.py`（headless schemathesis 启动器）、`tests/fuzz/test_o_fuzz_edges.py`（9 用例回归锁，`pytest` 9 passed）、`tests/fuzz/test_o_wall_time.py`（5 用例：O6 双端边界 + O1 模板统一源码锁）。
- 记账移交更新：O1→**已修**（模板统一 + degraded 头体同步 + evals marker 同步，见 §4 O1），O6→**已修**（422 边界校验，见 §4 O6），O2→**不修关票**（fail-closed 等价安全，见 §4 O2）。

### 5.1 Round 6 波次 2（W2 线，2026-10-21）：O3 / O4 / O5 三票收口

- src 修复：**无**（本波次**零 src 改动**；O3 判「有意设计」、O4 判「设计意图」、O5 判「框架层拒绝」三票均以**锁测试 + 文档**收口，不动 `src/hero_quant/api/security.py`、`src/hero_quant/infra/redis.py`）。
- 新增文件（本波次）：
  - `tests/fuzz/test_o3_scope_normalization.py`（**5 用例**：O3 scope 归一化锁——白名单 + 空白规范化 + 非法值 fail-closed；归一化后票据恰好 query 分域；签发端 HTTP 200/400；伪造载荷原始 `"query "` 不被识别为分域）。
  - `tests/fuzz/test_o4_redis_failclosed.py`（**5 用例**：O4 Redis fail-closed 锁——无后端 → 429 且不签票（caplog 锚定 `redis.ratelimit_no_backend_reject`）；后端抛错 → 503；security 层内存回退保持单次消费；`_require_real_redis_allowed()` env 矩阵；门禁关闭且无 DSN → `get_redis_sync()` 返回 None）。
  - `docs/fuzz-report.md`（本报告 §4 O3/O4/O5 + §5.1 + §7.1 更新）。
- 记账移交更新：O3→**已收口（有意设计 + 锁测试）**、O4→**已收口（设计意图 + 锁测试）**、O5→**已收口（框架层拒绝，非应用缺陷）**；三票均**不留悬案**。
- 遗留台账（本波次**不修**，仅记账）：
  - O3 附注：伪造票据载荷（原始 `"query "`）按 G-08 legacy 兼容降级为"无分域票"（迁移期全域放行）。前提是攻击者已能写入票据存储（Redis 属可信基础设施边界），不是票据消费路径的越权。若后续要收紧，需区分「legacy 无 scope 载荷」与「非法 scope 载荷」，并与 ADR-006 的 legacy 放行窗口截止日统一口径——超出本波次范围。
  - O4 附注：`_require_real_redis_allowed()` 在 pytest 进程内恒放行（`PYTEST_CURRENT_TEST`），生产无 DSN 时为 fail-closed；属预期，未改 `infra/redis.py`。
  - O5 附注：200KB URL 的上限在 uvicorn/httptools 解析层；如需放开应在网关/代理层调参（nginx `large_client_header_buffers`），不在应用层改。

## 6. 验收

- 请求数：live ≈330（schemathesis 256 + 定向 64 + 探针 ~10），pytest 9。
- 500：归零（0/330 live，0/9 pytest）。
- 复现条数：§3 共 13 条 curl 路径，每条可独立重放。
- 报告路径：`docs/fuzz-report.md`。
- 第二轮（O1/O6 修后复验，见 §7）：live 64（`scripts/fuzz_repro.py` 全量，500/异常=0）+ pytest 17（`tests/fuzz` 14 + `tests/test_ready_degraded.py` 3）+ evals fake headline 30/30 + `ruff check src` 通过。
- 第三轮（Round 6 波次 2 / W2 线，O3/O4/O5 收口，见 §7.1）：live 64（`scripts/fuzz_repro.py` 全量，500/异常=0，status 分布仅 1 例 429→400 的配额时序差异）+ pytest 24（`tests/fuzz`：既有 14 + 本波次新增 10；**离线 fake 模式 5.5s 全绿**，默认环境受限于本机无外网 + `.env` 真实 key 的 30s×重试网络阻塞，实测 38 分钟内 22/24 通过，见 §7.1）+ `ruff check src` 通过（**零 src 改动**）。

## 7. 修后复验记录（O1 已修 + O6 已修 422 + O2 不修关票）

- 代码提交：
  - `cbbd73b` — `O1: unify _FakeLLM templates + risk_warning marker (fuzz-report O1 done)`（`src/hero_quant/api/server.py` 两模板 hunk + `evals/run.py` 两处同步）。
  - `1bd8dc9` — `O6: wall_time_budget 422 boundary check + regression locks (fuzz-report O6 done)`（`src/hero_quant/api/server.py` 校验器 + 两调用点 + `import math`，`tests/fuzz/test_o_wall_time.py` 新建 5 用例）。
  - 本报告提交即 C3（`O2/docs: ...`）。
- 门禁数：
  - `pytest tests/fuzz tests/test_ready_degraded.py -q`：**17 passed**（`tests/fuzz/test_o_fuzz_edges.py` 9 + `tests/fuzz/test_o_wall_time.py` 5 + `tests/test_ready_degraded.py` 3）。
  - `python evals/run.py --mode fake`：**headline 30/30 = 100.0%**（cases=38，known_gap 8/8 通过，failure_classes={}）。
  - `ruff check src`：**All checks passed**。
  - live（`HERO_ENV=development`，`uvicorn hero_quant.api.server:app --port 8899`，`python scripts/fuzz_repro.py` 全量）：**total=64，status={400:15, 200:23, 429:1, 404:1, 405:7, 401:5, 422:10, 403:2}，failures(500/exc)=0**。§3 13 条 curl 逐条对照：F1 非法 scope→400（F1-9 遇 ticket 10/min 配额得 429，fail-closed 预期内）/ F1 空格归一化→200 / F2 超长 user→200×7 / F3 GET→404 + PUT/DELETE→405 / F4 401-200-401（单次语义）/ F5 200KB q→400 / F6 穿越→400×4 + 非法 wall_time→422×6 + 合法→200×2 / F8 offset -1→400 + abc→422 + 超大→200 / F9 risk→200 + degraded:true + reason。F6 非法 wall_time 现 422 属 O6 修复后预期；F3 GET 404 / PUT 405 属 O2 预期。
- 环境注记：`/ready` 在本离线环境返回 503 degraded（pg=false、checkpoint/billing 走 memory fail-closed），系无 PG/DSN 下的正确 fail-closed 行为，与首轮一致；服务本身全功能（POST 票据 200），不影响复验结论。

### 7.1 Round 6 波次 2（W2 线，2026-10-21）复验：O3 / O4 / O5 三票收口

- 改动分块（供总管**逐块**提交；本线**未**执行 `git add` / `git commit` / `git push`）：
  1. `tests/fuzz/test_o3_scope_normalization.py`（新建 5 用例，O3 锁）→ 建议 commit：`O3: lock ticket scope normalization semantics (fuzz-report O3 closed)`
  2. `tests/fuzz/test_o4_redis_failclosed.py`（新建 5 用例，O4 锁）→ 建议 commit：`O4: lock redis fail-closed ticket issuance + gate matrix (fuzz-report O4 closed)`
  3. `docs/fuzz-report.md`（§4 O3/O4/O5 + §5.1 + §7.1）→ 建议 commit：`C: docs/fuzz-report O3/O4/O5 closed (round6 wave2)`
- 门禁数：
  - `pytest tests/fuzz -q --no-cov`（**默认环境**）：**本波次新增 10 用例单独跑 10 passed in 4.51s**；全量 `tests/fuzz`（既有 14 + 新增 10 = 24）在**默认环境**下**极慢且未跑完**——本机**无外网**（`api.openai.com` / `1.1.1.1` 均连接超时），而 `.env` 提供了真实 API key ⇒ 服务端走真实 LLM（`src/hero_quant/api/server.py:1391-1412` → `ChatOpenAI(timeout=30)` + `LLMClient(max_retries=3)`）⇒ 每个 LLM 调用阻塞在 30s 网络超时 × 重试上。实测：38 分钟内 **22/24 用例已通过**（本波次新增 10 + `test_o_fuzz_edges.py` 9 + `test_o_wall_time.py` 前 3 个），第 23 例（既有 `test_o_wall_time.py` 的 stream 用例）仍在阻塞中，被人工终止。**该现象为改动前既有**：基线跑 `pytest tests/fuzz -q --no-cov -v`（改动前、14 用例）→ `test_o_fuzz_edges.py` 9/9 通过后卡在 `test_o_wall_time.py`，10 分钟无新进度、CPU 仅 8s（阻塞在 socket）⇒ 与本波次改动无关（本波次新增用例**不触发任何 LLM 调用**）。
  - `pytest tests/fuzz -q --no-cov`（**离线 fake 模式**，`HERO_API_KEY=" "`（单个空格）⇒ `server.py:1394-1395` `key.strip()` 为空 ⇒ 走 `_FakeLLM` 合成路径，与本报告首轮 "fake/offline LLM 模式" 同前提）：**24 passed in 5.52s**。
  - `ruff check src`：**All checks passed!**（本波次**零 src 改动**）；`ruff check tests/fuzz`：All checks passed!
  - live（`HERO_ENV=development` + `HERO_API_KEY=" "`（离线 fake 模式），`uvicorn hero_quant.api.server:app --port 8899`，`python scripts/fuzz_repro.py` 全量）：**total=64，status={400: 16, 200: 23, 404: 1, 405: 7, 401: 5, 422: 10, 403: 2}，failures(500/exc)=0**。
- 分布差异说明（与 §7 第二轮对比：**仅 1 例**从 429 → 400，其余 63 例逐条一致）：
  - 第二轮：`{400:15, 200:23, 429:1, 404:1, 405:7, 401:5, 422:10, 403:2}`；本轮：`{400:16, 200:23, 404:1, 405:7, 401:5, 422:10, 403:2}`。
  - 差异例：**F1-9**（`scope=Query`）。第二轮该例撞上 ticket 10/min 签发配额 → 429；本轮开跑前 60s 滑窗已清空（预探针距 F1 已 >60s），F1-9 拿到配额 → 走 scope 校验 → **400**（`"Query"` 大小写不匹配白名单，O3 语义：大小写敏感、非法即 400）。属**配额时序artifact**，非行为变化：本波次**零 src 改动**，其余 63 例状态逐条一致；access log 另见 3 次 429（`F2-0` 及 F5/F6/F7 内 `fresh_ticket` 的重试），均被脚本 `req_retry`/`fresh_ticket` 在 65s 后重试成功，故不计入最终分布。
  - O3 live 复核：**F1-3（`scope="query "`）→ 200**（其余 9 个非法 scope → 400），与 §4 O3 判定一致。
  - O5 live 复核：**F5-2（200KB `q`）→ 400**，uvicorn 侧仅 `WARNING: Invalid HTTP request received.`，**无 FastAPI access log、无应用异常、无 500**（本次 live 500 = 0）。
  - live 服务日志侧证：0 条 `"event": "query.failed"`；2 处 Traceback 均为**启动期非请求路径**（可选依赖 `mcp` 缺失、PG checkpoint 池 30s 超时 → emulated 降级），与请求路径无关。
- 500：归零（live 0/64；pytest 0/24）。
- 端口卫生：live server 跑完即 kill（`Stop-Process`），复查 `Get-NetTCPConnection -LocalPort 8899` → **无连接（已清理）**。
