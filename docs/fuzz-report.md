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

## 4. 根因分类与处理（O1/O6 已修，O2 文档确认；仅 O1/O6 触碰 src）

- **500/未捕获异常：0 条，无根因。**
- O1（degraded 标记不全，链A/D线归属）：**已修**。两处 `_FakeLLM` 模板以 `src/hero_quant/api/server.py:1444`（query 分支）为准统一 `src/hero_quant/api/server.py:1840`（stream 分支）：模板嵌入 `for query:{goal}`、尾部追加固定免责声明行 `本结果为合成占位，不构成投资建议`，并删除长模板多出的 3 行（数据来源/回测区间/等权策略跑赢基准）。body 侧 `degraded:true` + 非空 `degraded_reason` + `llm_mode=synthetic`（`src/hero_quant/api/server.py:1575-1604`），经 `_with_degraded_headers`（`src/hero_quant/api/server.py:184-192`）同步响应头 `X-Degraded/X-Degrade-Reason`；stream 首事件对应（`src/hero_quant/api/server.py:1975-1984` + 响应头 `src/hero_quant/api/server.py:2084-2089`）。`evals/run.py:39` 的 `FAKE_CHECKABLE` 加 `risk_warning` marker 与 FakeLLM 模板（`evals/run.py:52-55`）同步。回归锁：`tests/fuzz/test_o_wall_time.py:84-110`（源码级模板一致断言 + evals 对拍）。
- O2（GET /v1/query/ticket → 404，余者 405）：**不修关票**。唯一 POST 路由 `src/hero_quant/api/server.py:1614`；GET 落入 SPA catch-all（`src/hero_quant/api/server.py:2586-2594` / `src/hero_quant/api/server.py:2646-2651`）的 `v1/` 守卫（`src/hero_quant/api/server.py:2593-2594` / `src/hero_quant/api/server.py:2650-2651`）返回 404，fail-closed；PUT/DELETE 无路由命中由框架默认 405。404 与 405 均拒绝非 POST 方法，等价安全，无 500。锁定测试：`tests/fuzz/test_o_fuzz_edges.py:31-36`。
- O3（`scope="query "` → 200）：`security.py:104` strip 归一化，有意设计。**观察**，不修。
- O4（HERO_ENV 未设 + 无 Redis → ticket 全 429）：`infra/redis.py:220-237` 生产门禁 fail-closed，符合 G-06 精神；O 线起服用 `HERO_ENV=development` 走 fakeredis。**观察**（环境配置项，非缺陷）。
- O5（200KB q → 400 `Invalid HTTP request received`）：uvicorn/httptools 解析层拒绝超长请求行，未进 FastAPI，无异常日志冒泡。**观察**，不修。
- O6（`wall_time_budget=NaN/-5/1e9` → 200）：**已修 422**。API 边界为 `0 < x <= 3600`：`NaN/-5/0/3600.1/inf/-inf/1e9` → 422，合法 `1/3600` → 200。校验函数 `_validate_wall_time_budget`（`src/hero_quant/api/server.py:1321-1337`）置于限流（`src/hero_quant/api/server.py:1351` / `src/hero_quant/api/server.py:1721`）之后、ticket 消费（`src/hero_quant/api/server.py:1359` / stream 侧同序）之前：非法值直接 422 且不耗单次票（同一票连打非法值仍全 422、票仍可正常消费一次），无票 + 非法值时优先 422。只校验显式传入值（`None` 放行走 settings fallback：`src/hero_quant/api/server.py:1512-1514` / `src/hero_quant/api/server.py:1906-1908`，fallback 不钳制、不改默认）。`NaN` 须 `math.isnan` 显式拦截（比较符抓不住：`NaN > 0` 为 False 会落入旧归一化分支静默变 `None`，`src/hero_quant/agent/loop.py:365`；`inf` 同理经 `math.isinf` 拦截）。env 正数门保持：`src/hero_quant/config/settings.py:48-58`。回归锁：`tests/fuzz/test_o_wall_time.py:31-82`（query/stream 双端非法 422 + 合法 200 + 不耗票证据）。
- 工具链注记：schemathesis 4.32.1 CLI 在 Windows gbk 控制台必崩（rich `✅` 编码，`_win32_console.py:402`），与被测服务无关；O 线用 `scripts/st_run.py`（`is_terminal=False` monkeypatch） headless 运行，256 cases 全过。

## 5. 修复/记账清单

- src 修复：**有**——O1（`src/hero_quant/api/server.py:1444` + `src/hero_quant/api/server.py:1840` 两模板统一 + `evals/run.py:39,52-55` 同步）与 O6（`src/hero_quant/api/server.py:1321-1337` 校验器 + 两处调用点 `src/hero_quant/api/server.py:1354-1355` / `src/hero_quant/api/server.py:1724-1725`），另 `src/hero_quant/api/server.py:15` 增 `import math`（NaN/inf 拦截用）。
- 新增文件：`docs/fuzz-report.md`（本报告）、`scripts/fuzz_repro.py`（64 用例定向 fuzz，`--only F1..F9` 可选跑）、`scripts/st_run.py`（headless schemathesis 启动器）、`tests/fuzz/test_o_fuzz_edges.py`（9 用例回归锁，`pytest` 9 passed）、`tests/fuzz/test_o_wall_time.py`（5 用例：O6 双端边界 + O1 模板统一源码锁）。
- 记账移交更新：O1→**已修**（模板统一 + degraded 头体同步 + evals marker 同步，见 §4 O1），O6→**已修**（422 边界校验，见 §4 O6），O2→**不修关票**（fail-closed 等价安全，见 §4 O2）。

## 6. 验收

- 请求数：live ≈330（schemathesis 256 + 定向 64 + 探针 ~10），pytest 9。
- 500：归零（0/330 live，0/9 pytest）。
- 复现条数：§3 共 13 条 curl 路径，每条可独立重放。
- 报告路径：`docs/fuzz-report.md`。
- 第二轮（O1/O6 修后复验，见 §7）：live 64（`scripts/fuzz_repro.py` 全量，500/异常=0）+ pytest 17（`tests/fuzz` 14 + `tests/test_ready_degraded.py` 3）+ evals fake headline 30/30 + `ruff check src` 通过。

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
