# API 契约快照（OpenAPI Contract）

> 落地文件：`tests/test_openapi_contract.py`、`tests/contracts/openapi_snapshot.json`
> 应用入口：`src/hero_quant/api/server.py:523`（`app = FastAPI(title="hero-quant")`），
> 启动口径 `uvicorn hero_quant.api.server:app --port 8899` 见 AGENTS.md §3。

## 1. 这份快照解决什么问题

`/v1/*` 是对前端与外部消费方的**公开契约**。删一个 path、把某个 query 参数从可选改必填、
删掉一个响应字段、改一个状态码——这些改动**不会让任何既有单测变红**：那些测试各自只断言
自己关心的那条链路，覆盖面天然是局部的。等前端联调报 404/422 时，破坏性变更早已合入。

快照把 `app.openapi()` 的**表面形状**固化成一个可直接 diff 的文件，任何形状变化先在
`tests/test_openapi_contract.py` 里变红，把"要不要 breaking"从"联调事故"变成"review 决策"。

**快照不是 API 正确性证明。** 它只证明"表面契约没偷偷变"，不证明：

- 状态码的语义正确（`200` 里返回的是不是该返回的东西）；
- 鉴权 / 计费 / 降级是否按 AGENTS §6 的 fail-closed 生效；
- 字段含义、单位、错误码语义是否变化（改注释、改实现、改 SQL 都不进 schema）。

这些仍由各自的行为测试与 `docs/issues.md` 里的验收项负责。快照与行为测试是**互补**关系，
不是替代关系。

## 2. 快照是什么、怎么来的

| 项 | 内容 |
| --- | --- |
| 来源 | `app.openapi()`（FastAPI 由路由+签名自动生成，见 `server.py:523`） |
| 文件 | `tests/contracts/openapi_snapshot.json` |
| 序列化 | `json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=2)` + 末尾换行 |
| 规模（c0b40aa 基线） | 885 行 / 24415 字节；17 个 path / 17 个 operation / 2 个响应模型 |
| 确定性 | 同一份代码两次生成**字节相同**（清缓存重建也相同，见 `test_openapi_generation_is_deterministic`） |

为什么这三个序列化参数不能省：

- `sort_keys=True`：消除 dict 迭代序差异，`PYTHONHASHSEED` 变了也不会假红；
- `indent=2` + 末尾换行：让 `git diff` 是逐行可读的键级 diff，而不是一行 23KB 的 JSON；
- `ensure_ascii=False`：中文描述原样落盘，review 时能直接看懂改了什么。

`info.version` **不做**占位符规范化（不写成 `"<version>"`）。版本号从 `pyproject.toml` /
`FastAPI(version=...)` 单源变化时，本来就该在契约 diff 里可见——把它抹平等于把"发版口径变了"
藏起来。

## 3. 什么算破坏性变更

判定口径（也是 `_severity_*` 的标签依据，见 `tests/test_openapi_contract.py` 的
`_severity_removed` / `_severity_added` / `_severity_changed`）：

| 类别 | 变更 | 严重度 | 受影响方 |
| --- | --- | --- | --- |
| path | 删掉一个 path | BREAKING | 前端路由、SDK、探针 |
| method | 删掉某 path 上的 method | BREAKING | 同上 |
| 参数 | 删掉一个 query/header 参数 | BREAKING | 依赖该参数的调用方 |
| 参数 | 新增**必填**参数 | BREAKING | 未传该参数的全部存量调用方（直接 422） |
| 参数 | 新增可选参数 | ADDITIVE | 无 |
| 必填字段 | `required: false -> true` | BREAKING | 未传该参数的调用方 |
| 必填字段 | 必填性任一方向变化（含响应模型 `required` 成员增减） | BREAKING | 保守提示：字段可能缺失或调用方必须补参，由人降级 |
| 类型 | `schema.type` / 嵌套 `type` 变化（如 `integer -> string`） | BREAKING | 解析层直接崩 |
| 响应状态码 | 删掉一个已声明的状态码（尤其 `200`） | BREAKING | 客户端状态机分支失效 |
| 响应状态码 | 新增状态码（如多一个 `503` 降级分支） | ADDITIVE | 需前端补横幅/分支，但不打断现有调用 |
| 响应模型 | 删 `components.schemas.*` 的 property | BREAKING | 读取该字段的前端/报表 |
| 响应模型 | 新增 property | ADDITIVE | 无 |
| 文档 | `description/summary/title/tags` 文本变化 | COSMETIC | 无（但不许夹带泄漏，见 §5） |

严重度只是**提示**，不是裁决：任何 diff 都会让测试变红，最终是否接受由 review 判定并在 PR
里留痕。

## 4. 更新流程（五步，必须走代码路径）

```bash
# 1) 改 API（src/hero_quant/api/server.py 或路由模块），跑契约测试看差异
PYTHONPATH=$PWD/src .venv/bin/python -m pytest -q tests/test_openapi_contract.py
#    -> 失败时会打印结构化 diff：类别 + 键路径 + 旧值 -> 新值

# 2) 人工判定：这些变化是否属于有意变更？下游（frontend/、SDK、报表）是否需要同步？
#    破坏性变更必须在 PR 描述里显式说明并给出迁移方案。

# 3) 确认后重新生成快照（唯一合法方式；禁止手改 JSON）
UPDATE_OPENAPI=1 PYTHONPATH=$PWD/src .venv/bin/python -m pytest -q tests/test_openapi_contract.py
#    -> 只重写快照并 skip（不会静默通过），提示人工 review diff

# 4) 复核 git diff（必须是键级、可逐行读的）
git diff -- tests/contracts/openapi_snapshot.json

# 5) 把 §3 的判定结论与 diff 摘要贴进 PR 描述
```

规则：

- `UPDATE_OPENAPI=1` 之后**必须**再跑一次默认模式，确认快照与当前 schema 严格相等；
- 快照只由 `app.openapi()` 生成。手工编辑、别的工具重排、外部拉取的 `openapi.json` 一律
  不允许——`test_snapshot_file_is_canonically_formatted` 会拦住非规范排版；
- 换分支/改路由后如果快照"莫名"红，先看 diff，不要第一反应就 `UPDATE_OPENAPI=1`。

## 5. 与快照无关的独立不变量

这些断言不读快照，即使快照被整体重生成也照样生效（见 `tests/test_openapi_contract.py`）：

1. 关键路径存在：`/v1/query`、`/v1/query/stream`、`/v1/backtest/metrics.json`、`/v1/audit`、
   `/live`、`/ready`；
2. 所有 `operationId` 唯一且非空（客户端代码生成依赖它）；
3. 每个 operation 至少声明一个 `2xx` 响应；
4. `servers` 与描述性字段里不出现 `example.com` / `localhost` / `127.0.0.1` /
   `10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16` 内网地址（信息泄漏 + 联调误导）。

### 派单与实现的偏差：`/health` 不存在

PR-182 派单要求的关键路径里写了 `/health`，但本仓**没有注册** `/health` 路由——它只作为
匿名放行白名单项存在：

```bash
# 唯一命中：鉴权中间件的公开路径白名单（预留 / 反代兼容），不是路由
$ grep -rn '/healthz\?"' src/
src/hero_quant/auth/middleware.py:29:    "/health",
src/hero_quant/auth/middleware.py:30:    "/healthz",

# 没有任何 health 路由注册
$ grep -rn '@app\.\(get\|post\)("/health' src/     # 无命中

# 真正的存活 / 就绪探针
$ grep -n '"/live"\|"/ready"' src/hero_quant/api/server.py
1313:@app.get("/live")
1811:@app.get("/ready")
```

也就是说 `DEFAULT_PUBLIC_PATHS`（`src/hero_quant/auth/middleware.py:27-31`）把 `/health`、
`/healthz` 当作免鉴权路径预留了，但 `app.routes` 里并没有对应 handler，`app.openapi()` 自然也
不会出现它们。**契约按仓库实际锁 `/live`**（存活）与 `/ready`（就绪，AGENTS.md §3「`/ready` 绿」），
不去断言一个不存在的路由；若后续确实要新增 `/health` handler，那就是新增 path，按 §4 流程走一遍即可。

## 6. CI 尚未接入（可复制片段）

本批白名单不含 `.github/`，所以契约门目前**只在本地跑**。CI 里要接的话，最省事的是在既有
pytest 任务里多挂一个显式步骤（这样失败原因一眼可见，不会被其他用例的长日志淹没）：

```yaml
# 片段：接在既有 pytest 步骤之后；不要另起 workflow，避免重复装依赖
      - name: OpenAPI contract (snapshot + invariants)
        env:
          PYTHONPATH: ${{ github.workspace }}/src
        run: |
          .venv/bin/python -m pytest -q tests/test_openapi_contract.py
```

要点：

- **不要**在 CI 里设 `UPDATE_OPENAPI=1`（那会让门禁自我修复，等于关掉）；
- 失败时 pytest 会把结构化 diff 打到日志里（`test_snapshot_matches_current_schema` 用
  `pytest.fail(report, pytrace=False)`，不展开 traceback）；
- 若要强制"破坏性变更必须人工签字"，可再加一个 PR label 检查，把 §3 的 BREAKING 行作为
  必须出现在 PR 描述里的证据。

## 7. 维护约定

- 改任何路由/签名/响应模型 => 同一 PR 内更新快照，并贴 diff；
- 新增 path 优先登记进 `_REQUIRED_PATHS`（如果它是关键链路），否则靠快照覆盖即可；
- 不允许为了让契约测试过而在 `src/` 加"测试开关"——测试只读 `app.openapi()`，一旦它开始依赖
  真实 PG/Redis 连接或副作用，应当停下来排查（本文件落地时 `app.openapi()` 完全离线可跑，
  不连 PG/Redis，只打印 `mcp` 可选依赖缺失与 `frontend/dist` 缺失的既有告警）。
