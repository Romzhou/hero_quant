# Memory 模块深挖（文件 + SQLite-FTS5 + pgvector 回退）

> 依据：`docs/recon.md` §1/§3（存储）、`docs/issues.md` P3-3。行号为 2026-10-19 快照实测值。

## 1. 职责一句话

`src/hero_quant/memory/` 对外提供记忆写入/检索/去重：文件为真实来源（可审计、支持层次路由），SQLite-FTS5 为检索索引（BM25，trigram 优先），向量检索本地优先、pgvector sidecar 已配置时为一等公民且失败静默回退本地。

## 2. 关键文件与行号

| 文件 | 行 | 内容 |
|---|---|---|
| `src/hero_quant/memory/store.py:1-4` | 模块 docstring | “文件 + SQLite FTS5 双存储与向量混合检索；文件为真实来源；30 秒滑动窗口去重；向量本地优先、pgvector 失败静默回退” |
| `src/hero_quant/memory/store.py:48` | trigram 切分 | 相邻二字 token，供无 trigram 时 FTS5 回退 |
| `src/hero_quant/memory/store.py:61-88` | DSN 收口 | pgvector DSN 前缀白名单（`:61`）；`_pgvector_dsn(:65)` 经 Settings 解析，非法 → `None`；`is_pgvector_configured(:79)` 鉴权收口 Settings |
| `src/hero_quant/memory/store.py:104-113` | 向量字面量 | `to_pgvector_literal`（委托 `hero_quant.agent.embed`，`:111`），兼顾 vector 类型与 TEXT 回退 |
| `src/hero_quant/memory/store.py:123-130` | `class PgVectorSidecar` | “配置时一等公民，未就绪则静默回退到本地”（docstring `:123`）；`dsn` 经 `_pgvector_dsn()`（`:130`） |
| `src/hero_quant/memory/store.py:211,228,245,257,268,300-301,341-342,363-364,444-445,1025-1026,1056-1057` | 静默回退点 | 均为 `logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=...)` + `pass # intentional offline-safe`（抽查 13 处，与 recon “向量失败必须回退”一致） |
| `src/hero_quant/memory/store.py:387` | `def search` | 向量检索入口（`query_vec/top_k/namespace`） |
| `src/hero_quant/memory/store.py:475-476` | `class MemoryStore:` | 主存储：文件落地 + FTS5 索引 + 30 秒去重 + 向量混合检索 |
| `src/hero_quant/memory/store.py:502-519` | sidecar 装配 | 已配置 → `PgVectorSidecar(dim=get_vector_dim())`（`:508`）；否则 `_pgvector=None/_enabled=False` 回退本地（`:515-519`） |
| `src/hero_quant/memory/store.py:643-655` | 后端自述 | `vector_backend` → `pgvector/local`（`:643-646`）；`is_pgvector_enabled(:653)` 三条件与门 |
| `src/hero_quant/memory/store.py:739,777` | SQLite 初始化 | 建表 + 补向量列 + 创建 FTS5 虚表（`:777`） |
| `src/hero_quant/memory/store.py:1044-1057` | `_pgvector_upsert` | 未启用/无 vec 直接返（`:1046`）；失败 `debug + pass`（`:1056-1057`） |
| `src/hero_quant/memory/__init__.py:4` | 架构位置 | “上游为 Agent 工具与上下文，下游落盘到文件目录 + SQLite FTS5 + 可选 pgvector” |
| `src/hero_quant/memory/__init__.py:16` | 导出 | `MemoryStore, PgVectorSidecar, get_vector_dim, is_pgvector_configured` |
| `src/hero_quant/memory/lifecycle.py:125,129,133,183,188,204,335,523` | 裸 `pass` 群 | P3-3 抽查命中（与 `docs/issues.md` P3-3 一致，`wall_time` 相关） |
| `src/hero_quant/memory/hierarchy.py:249,262,340` | 裸 `pass` 群 | P3-3 抽查命中 |
| `src/hero_quant/memory/store.py:115,229,246,258` | `pass` 群 | P3-3 抽查命中（总量级与“20 处”一致） |

## 3. 上游 / 下游

- 上游：Agent 工具与上下文（`AgentLoop.__init__` 注入 `memory_store`，见 `loop.py:119-134`；`__init__.py:4`）。
- 下游：文件目录（真实来源）→ SQLite `memory.db` FTS5 索引（`:739-777`）→ 可选 pgvector sidecar（`:502-519`）。

## 4. 检索优先级与一致性

1. 文件为真实来源（可审计、层次路由）；2. FTS5 为 BM25 索引（trigram 优先，`:48`）；3. 向量本地优先；4. pgvector 已配置且就绪时为一等公民（upsert/query 双写/双读，失败任一环节即回退本地，不抛错）。
- 去重：30 秒滑动窗口对归一化 content 去重（`:4` docstring）。

## 5. 降级语义

| 场景 | 语义 | 依据 |
|---|---|---|
| pgvector 未配置 | 回退本地混合检索；`is_pgvector_enabled()=False`，`vector_backend()=local` | `store.py:506-519,643-655` |
| pgvector 运行时失败 | **静默回退**（`debug + pass # intentional offline-safe`）：离线安全，不断检索。注意与 billing/checkpoint 的 fail-closed 相反——记忆向量是“可选增强”，降级不抛错是设计意图 | `store.py:211` 等 13 处 |
| FTS5 无 trigram | 二字 token 回退（`:48`），检索召回下降但可用 | `store.py:48` |
| DSN 非法 | `_pgvector_dsn()=None` → 视为未配置，走本地 | `store.py:65-88` |

## 6. 已知问题挂钩

- P3-3 `memory/` 20 处量级裸 `pass`（`lifecycle.py/hierarchy.py/store.py` 行号见 §2）：要求“单测锁定一处、清理一处，不得新增裸 pass”。注意区分：`store.py` 的 pgvector 回退 `pass` 均带 `intentional offline-safe` 注释 + `debug` 日志，属于已声明的降级语义，清理时应保留语义、补单测而非简单删除；`lifecycle.py/hierarchy.py` 的 `wall_time` 裸 `pass` 才是优先清理对象。归 B（锁定）+ D（清理）。
