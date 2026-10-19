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

## 4. 根因分类与记账（全部只记账，零 src 落地）

- **500/未捕获异常：0 条，无根因。**
- O1（degraded 标记不全，链A/D线归属）：本环境 `langchain_openai` 缺失 → `factory.py:130` dummy key + `_FallbackChat` 回显，`server.py:1419` 的 `_FakeLLM` 分支未被命中（`llm is None` 永假），导致 `llm_mode: synthetic` 缺失、`degraded_reason` 仅含 checkpoint 原因。`degraded:true` 本体仍在，不算假成功，但与“`_FakeLLM` 必须置 degraded+reason”（AGENTS §6）存在缺口。**记账**：`src/hero_quant/api/server.py` 在 K 线 diff 集合内（c8c6487/cbe6997/f68052f），O 线无权改；且任务行号（1091-1401/229-235）已漂移，实际 `_FakeLLM` 在 1420-1435。请 D 线认领。
- O2（GET /v1/query/ticket → 404，余者 405）：SPA catch-all `GET /{full_path:path}`（`server.py:2562-2570`/`2622-2627`）的 `v1/` 守卫返回 404，fail-closed，等价安全。**记账**，不修（行为一致、无 500）。
- O3（`scope="query "` → 200）：`security.py:104` strip 归一化，有意设计。**观察**，不修。
- O4（HERO_ENV 未设 + 无 Redis → ticket 全 429）：`infra/redis.py:220-237` 生产门禁 fail-closed，符合 G-06 精神；O 线起服用 `HERO_ENV=development` 走 fakeredis。**观察**（环境配置项，非缺陷）。
- O5（200KB q → 400 `Invalid HTTP request received`）：uvicorn/httptools 解析层拒绝超长请求行，未进 FastAPI，无异常日志冒泡。**观察**，不修。
- O6（`wall_time_budget=NaN/-5/1e9` → 200）：FastAPI 解析通过，AgentLoop 内消化（_FallbackChat 即时返回），无挂起无 500。语义层是否应 422 属产品决策，**记账**给 D 线，不修。
- 工具链注记：schemathesis 4.32.1 CLI 在 Windows gbk 控制台必崩（rich `✅` 编码，`_win32_console.py:402`），与被测服务无关；O 线用 `scripts/st_run.py`（`is_terminal=False` monkeypatch） headless 运行，256 cases 全过。

## 5. 修复/记账清单

- src 修复：**无**（`server.py`/`security.py` 均在 K 线 diff 集合：`src/hero_quant/api/server.py`、`src/hero_quant/api/security.py`、`src/hero_quant/infra/session.py`；且 fuzz 未发现任何需 fail-closed 小修的 500）。
- 新增文件：`docs/fuzz-report.md`（本报告）、`scripts/fuzz_repro.py`（64 用例定向 fuzz，`--only F1..F9` 可选跑）、`scripts/st_run.py`（headless schemathesis 启动器）、`tests/fuzz/test_o_fuzz_edges.py`（9 用例回归锁，`pytest` 9 passed）。
- 记账移交：O1→D 线（链A 降级标记），O6→D 线（wall_time 语义），O2→C 线（文档可选注记 GET 票据路由 404）。

## 6. 验收

- 请求数：live ≈330（schemathesis 256 + 定向 64 + 探针 ~10），pytest 9。
- 500：归零（0/330 live，0/9 pytest）。
- 复现条数：§3 共 13 条 curl 路径，每条可独立重放。
- 报告路径：`docs/fuzz-report.md`。
