# Scan Remain 全量修复设计 — 916 条质量债

**日期**: 2026-08-28  
**扫描源**: `scan_remain.log` — 187 files / 916 comments (去重后)  
**基线**: P0 S1 已完成 4 文件安全修复（approval/credentials/redaction/tools/redaction），已纳入本设计基线  
**决策**: C 全量清零 + A 2周分批 + B 风险清零 + C 可顺手优化 + A 分级×分域并行（worktree隔离）

---

## 1. 现状与分拣

### 1.1 规模
- **critical 13**: bug 8 + security 5 — 阻塞发布
- **high 298**: bug ~180 + security ~30 + performance ~15 + other
- **medium 448**: 其中 bug/security 170
- **low 157**: style/docs/other
- **按类型**: bug 342, test 248, maintainability 194, security 58, performance 31, other 28

重灾区 Top: `Research.tsx 17`, `checkpoint/postgres 13`, `security/approval 11`, `Chat/Live/Monitor 11/11/7`, `quantlib/rust 10`, `backtest/bench 10`, `data/registry 9`, `scheduled/service 9`

### 1.2 13 条 Critical 清单（P0）
1. `quantlib/rust.py:88` NaN→0.0 污染指标
2. `checkpoint/postgres.py:110` `hash()` 随机导致 checkpoint 不可恢复
3. `data/loaders/yahoo.py:83` 宽 `except` 伪装 ImportError
4. `data/registry.py:246` synthetic/live 溯源三处不一致
5. `llm/client.py:49` `stream_chat` 已 yield 后重试导致重复输出+泄漏
6. `security/approval.py:113` 策略降级 `ask→auto` 绕过（已修✓）
7. `security/credentials.py:121` TOCTOU + 0644 窗口泄露（已修✓）
8. `security/redaction.py:112` RESULT_SINK 旁路泄露（已修✓）
9. `shadow/service.py:118` 熔断器 fail-open 放行
10. `tools/correlation.py:40` 合成假数据伪装成功
11. `tools/redaction.py:16` fail-open 泄露（已修✓）
12. `tests/test_otel_maturity3:93` `sys.modules.clear()` 破坏全局导入
13. `frontend/Risk.tsx:31` 静默 catch 用假数据伪装风控正常

> 已修 4 项为 `fix-1` 产出，`py_compile` 全过，fail-closed 已验证。剩余 9 项纳入 P0 批次。

---

## 2. 目标与约束（Brainstorm 结论）

- **目标**: C 全量 916 条可追溯清零
- **工期**: A 2周内分4批，P0→P1→P2→P3 顺序，每批独立分支+回归，worktree 隔离
- **验收**: B 风险清零 — critical/high 必须 0 遗留（重跑同规则扫描验证），medium/low 允许 ≤10% 误报 `ocr-ignore` 白名单，相关单测不跌
- **边界**: C 可顺手优化 — 允许对重复逻辑抽公共函数/常量与小性能优化，不引入新框架/不改数据模式

---

## 3. 方案对比（已选 A）

| 方案 | 描述 | 优点 | 缺点 | 适配度 |
|------|------|------|------|--------|
| **A 分级×分域并行（推荐/已选）** | 4 批次 × 5 域并行，worktree隔离，每批两阶段 review | 2周可交付、critical/high 优先清零、合并风险低、并行度高 | 需协调 5 lanes、需两次重跑扫描 | ★★★★★ 完美匹配 C+A+B+C |
| B 单分支串行 | 单分支按文件顺序串行 916 | 合并简单、历史线性 | 2周无法完成、critical 与 low 混修阻塞发布 | ★★ |
| C 按文件类型分片 | 前端/后端/测试 3片并行 | 关注点分离 | 忽视严重度、critical 可能被排到后期 | ★★ |

---

## 4. 架构总览 — 分级×分域并行

```
Batch P0 (critical 13, ~3天) ─┬─ S-sec: security/approval, credentials, redaction ✓已修
                              ├─ S-data: quantlib/rust, checkpoint hash, yahoo, registry
                              ├─ S-llm: llm/client stream, shadow breaker, correlation, otel test
                              └─ D-risk: Risk.tsx 关键路径（@designer）

Batch P1 (high 298-13=285, ~5天) ─┬─ A-sec: 剩余 security/billing/sql 注入
                                  ├─ B-data: loader 硬编码回退、0值 falsy、board_lots、scope 循环
                                  ├─ C-engine: backtest engine/metrics/validation/billing
                                  ├─ D-frontend: Chat/Live/Monitor/Dashboard SSE泄漏、重连风暴、静默吞错
                                  └─ E-link: stream/telemetry/mcp/quantlib 剩余

Batch P2 (medium 448, ~4天) ── 170 bug/security medium 优先，剩余 medium 分域清理
Batch P3 (low+maintainability+test ~194+157+248, ~2天) ── 常量抽取、嵌套三元、magic number、白名单标注
```

**写冲突隔离**: 每 lane 独占文件集合，不跨 lane 写同一文件。P0 剩余 9 文件与已修 4 文件无交集，可并行。

---

## 5. 组件与职责

| Lane | Owner | 文件域 | 典型修复 |
|------|-------|--------|----------|
| **S-sec** | @fixer | `security/*`, `tools/redaction`, `checkpoint/postgres:267` SQL拼接 | 策略 ceiling、原子 0600 写入、RESULT_SINK 递归脱敏、参数化 SQL |
| **S-data** | @fixer | `quantlib/rust`, `data/loaders/*`, `data/registry/trait`, `core/scope` | NaN 保留、hash→hashlib、yahoo 窄化 except、溯源统一、falsy→is None |
| **S-llm** | @fixer | `llm/client`, `shadow/service`, `tools/correlation`, `tests/otel` | stream 重试仅首包前、breaker fail-closed、合成数据标 provenance、mock.patch.dict |
| **D-risk/frontend** | @designer + @fixer | `frontend/pages/*`, `index.css`, `index.html` | AbortController、unmount 清理、SSE 重连退避、CSV 健壮解析、iframe sanitize、常量抽取 |
| **E-link** | @fixer | `stream/*`, `telemetry/*`, `mcp/*`, `scheduled/*`, `config/*` | 心跳/限流/阈值常量化、limits 截断修复 |

---

## 6. 数据流与批次流

1. **输入**: `scan_remain.log` → 解析 `─── file:line ─── [type·severity]` → 归类 severity×domain
2. **分批**: P0(13) → P1(285 high) → P2(448 medium) → P3(低+债)，每批独立 `worktree` 分支 `fix/scan-p{0..3}-{lane}`
3. **执行**: 每 lane TDD — 先写失败单测（复现 critical/high），再改实现，再绿，最后 `py_compile` + 域内单测
4. **门禁**: 每批完成后重跑 `open-code-review` 同规则扫描（抽样）+ `pytest -k <domain>` + 前端 `vitest`
5. **归并**: `oracle` 总体复审（熔断/审批/回测对齐），`B` 验收标准判定，通过后合入 `main`

---

## 7. 错误处理策略

- **Fail-closed 优先**: 审批/熔断/脱敏/合成数据等安全/数据完整性路径，异常时拒绝或 `***`，不静默 `pass` 或返回假数据
- **不吞错**: 移除 `except Exception: pass/continue`，替换为 `logging.warning(..., exc_info=True)` + 明确回退或抛错
- **资源不泄漏**: `AbortController`/`reader.cancel()`/`releaseLock()`/`gen.close()`/`clearTimeout` 成对清理，`useEffect` 卸载必 abort
- **确定性**: `hash()`→`hashlib.sha256`，`Date.now()`→`crypto.randomUUID()`，时区统一 UTC

---

## 8. 测试策略（TDD 强制）

- **每任务**: 写失败测试 → 看红 → 最小实现 → 看绿 → 提交
- **P0/P1**: 为每条 critical/high 补复现单测（如 `test_checkpoint_hash_deterministic`, `test_approval_ceiling`, `test_rust_nan_preserved`, `test_risk_error_banner`）
- **域回归**: `pytest tests/test_checkpoint* tests/test_approval* tests/test_quantlib* -n auto`，前端 `vitest --run`
- **扫描回归**: 每批后 `npx @alibaba-group/open-code-review@1.11.0 --include <batch files>` 抽样，确保对应规则 0 遗留
- **不做全量扫描习惯性重跑**: 仅批内文件+关联域，按风险扩散按需扩大

---

## 9. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 916 条中存在误报（hardcode 误判、style 主观） | P3 允许 10% `// ocr-ignore` 白名单，需在 PR 说明理由，oracle 复审 |
| 多 lane 并行写冲突 | Worktree + 文件集合互斥清单，pre-merge `git diff --name-only` 校验 |
| 回测/量化修复引入数值漂移 | 为指标计算加 golden 回归（`test_backtest_golden`），NaN 处理前后对比 |
| 前端大量 SSE/状态重构引入回归 | @designer 统一常量/Abort 方案，@fixer 仅机械收尾，保留视觉意图 |

---

## 10. 交付物与分支

- 设计: `docs/plans/2026-08-28-scan-remain-design.md`（本文件）
- 计划: `docs/plans/2026-08-28-scan-remain.md`（下一步产出，TDD 任务级拆分，2–5 分钟/任务）
- 分支: `fix/scan-p0-critical`, `fix/scan-p1-high-{sec,data,engine,frontend,link}`, `fix/scan-p2-medium`, `fix/scan-p3-debt`
- 基线提交: `fix-1` 的 4 文件已在本设计基线中，下一步 `git add` 并与设计同提交

---

## 11. 审批记录

- Brainstorm: C/A/B/C 已确认（2026-08-28）
- 方案: A 分级×分域并行 已选
- 待批: 本设计章节 → 撰写实施计划 → Subagent-Driven Build

