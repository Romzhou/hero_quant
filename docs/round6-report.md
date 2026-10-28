# Round 6 收口报告（8 commit + 终态门禁 + 10 条遗留台账）

> 性质：**docs-only 记账**。本文件只做汇总与索引，**不推翻任何既有文档的结论与数字**；所有数字给出出处（`file:line`）或可重跑命令，文末 §7 附核验命令清单。
> 边界：本线只新建本文件 + 在 [fuzz-report.md §4](fuzz-report.md)、[mutation-audit.md](mutation-audit.md)、[adr/README.md](adr/README.md)、[agent-eval-real.md](agent-eval-real.md) 四篇末尾**追加**交叉引用段；`src/**`、`tests/**`、`evals/**`、`migrations/**`、`.github/**` 零改动。
> 时点：2026-10-28，基线 `d1715db`（Round 7 前置基线），HEAD `5a2466b`（Round 7 X1 线 PG 守卫）。

## 1. Round 6 的 8 条 commit（逐条 `git log --oneline -1` 核验）

标题取自 git 实际输出，未改写。8 条均已用 `git merge-base --is-ancestor <hash> d1715db` 确认落在基线历史内。

| # | hash | 标题（git 原文） | 一句话内容 |
|---|---|---|---|
| 1 | `7b4dae8` | docs: 台账收敛（mutation-report 计数/等价性订正 + 三篇相关文档互链） | 订正 `docs/mutation-report.md` 的存活计数与等价性表述，并把 mutation-report / mutation-audit / fuzz-report 三篇互链。 |
| 2 | `67735e8` | docs(mutation-audit): id69 算子坐实 + id72/id76 编号订正 + F1 重新归因 | 用 harness `results.json` 坐实 id69 = `> -> !=`，订正 id72/id76 编号，把真缺口重新归因到 id65/id81（见 [mutation-audit.md §2](mutation-audit.md)）。 |
| 3 | `763ed38` | docs(adr): ADR-005 billing tenant 归属 + ADR-006 per-user 身份与鉴权重构 | 新增两条 ADR 决策记录（状态均为「已定方向，未实施」，零代码改动，见 [adr/README.md:44-52](adr/README.md)）。 |
| 4 | `f083568` | O3: lock ticket scope normalization semantics (fuzz-report O3 closed) | O3（`scope="query "` → 200）判「有意设计」→ 新增 `tests/fuzz/test_o3_scope_normalization.py`（5 用例）锁语义，**零 src 改动**。 |
| 5 | `bf4adbf` | O4: lock redis fail-closed ticket issuance + gate matrix (fuzz-report O4 closed) | O4（无 Redis → ticket 429）判「设计意图」→ 新增 `tests/fuzz/test_o4_redis_failclosed.py`（5 用例）含 `_require_real_redis_allowed()` env 矩阵，**零 src 改动**。 |
| 6 | `78569c1` | C: docs/fuzz-report O3/O4/O5 closed (round6 wave2) | `docs/fuzz-report.md` §4 O3/O4/O5 收口 + §5.1 记账 + §7.1 复验（live 64，500=0）。 |
| 7 | `d556b08` | test: W4 branch coverage for backtest/quantlib/llm/agent (67.85% -> 70.36%, +821 cases) | W4 分支补测：新增 31 个 `tests/test_w4_*.py`（`git show --stat` → 31 files changed, 8303 insertions(+)，`src/` 零改动）。 |
| 8 | `8301ba2` | docs: validation-report W4 补记（覆盖率口径 + 4 项 src 差异记账） | `docs/validation-report.md` §W4 补记：覆盖率口径 + 四块目录表（`:137-144`）+ W4-1..4 差异记账（`:164-171`）+ W4 遗留台账（`:173-186`）。 |

用例数口径校正（可复核）：`d556b08` 新增文件里 `def test_` 函数共 **768** 个（逐文件 `git show d556b08:<path> | grep -c '^def test_'` 求和），参数化展开后 pytest 收集 **821** 用例——两者不矛盾：`821` 是 collected 用例数，`768` 是函数数。

## 2. Round 6 终态门禁数字

| 门 | 命令 | 结果 | 出处 |
|---|---|---|---|
| 全量测试 | `pytest -q` | **2540 passed / 0 failed / 7 skipped / 44 deselected**（collected 2591） | Round 6 收尾门禁输出（本文件转记，见下方「诚实边界」） |
| 覆盖率 | `pytest -q --cov=src` | **70.36%**（covered **17719** / stmts **25183**） | `docs/validation-report.md:134` |
| lint（唯一真门） | `ruff check src` | **All checks passed!** | 本轮（2026-10-28）复跑确认；与 `docs/validation-report.md:162` 一致 |
| lint（全仓，不在门内） | `ruff check .` | Found **413** errors（198 fixable） | 本轮复跑确认；历史 lint 属已知豁免，**不进 Round 6 门禁**，也不构成回归 |
| 评测 | `python evals/run.py --mode fake` | headline **30/30**、known_gap **8/8**、`failure_classes={}` | `docs/agent-eval-real.md:29`、`:44`；`docs/fuzz-report.md:107` |

数字自洽校验：`2540 + 7 + 44 = 2591`（passed + skipped + deselected = collected），算式闭合。

**诚实边界（必须一起读）**：pytest / coverage 两组数字是 **Round 6 收尾门禁输出**的转记，本文件写作时**未复跑全量套件**（本机全量约 20 分钟，且 Round 7 有并发跑批），因此本文件**不为这两行提供新的实测**。另：Round 7 X1 线（`5a2466b`）已把历史上那 44 例从「`--deselect` 排除」改成「守卫式 skip」（`git show --stat 5a2466b` → `tests/conftest.py` +150、5 个测试文件各 +9~12），故 Round 7 收尾的 `pytest -q` 数字**必然与上表不同**（skipped 会增加、deselected 应归零），届时以 Round 7 报告为准，本文件不预支。

## 3. 覆盖率口径（W4 = `d556b08`）

- 整体：**67.85% → 70.36%**（covered 17087 → 17719，stmts 25183 不变），净增 covered **+632**，四个目标目录合计 **+600**（`docs/validation-report.md:133-135`）。
- 规模：新增 **31** 个测试文件、**821** 用例（其中 1 例因 Windows 无符号链接权限被 skip），`src/**` 零改动（`docs/validation-report.md:128-130`）。
- **对称 deselect，前后可比**：5 个依赖真实 PG 的测试文件（`tests/test_0904_c1.py`、`tests/test_checkpoint_pg.py`、`tests/test_j8_loop_ckpt_gov.py`、`tests/test_retrieval_eval.py`、`tests/test_vector_maturity4.py`，共 **44** 用例）在**基线跑与终态跑都用同样的 `--deselect` 排除** → 覆盖率前后可比（依据 `docs/validation-report.md:156-161`；派单提示的 `157-163` 经核验实际为 **156-161**，行号已按文件实际订正）。
- **四块目录提升表不复制**：`backtest/` `quantlib/` `llm/` `agent/` 四行的 stmts / 基线覆盖 / 最终覆盖 / 提升 / 新增 covered 一律以 `docs/validation-report.md:137-144` 原文为准，本文件不二次转写，避免转写失真。
- 口径沿革（读者须知）：`docs/validation-report.md:126` 起是「Round 6 波次 2」的 W4 补记；`6dea1f0`（更早）统一过 turnover 口径，两者不冲突（前者是覆盖率，后者是业务语义）。

## 4. 遗留台账（10 条）

「当前状态」= 现在代码/文档的事实；「收口计划」= 下一步动作；「人拍板」= 是否需要用户/总管决策（`否` 指可工程内自行收口）。第 4–7 项的行号与现象均**回文件 `sed` 核验**过，标注「已核验」。

| # | 编号 | 现象 | 当前状态 | 收口计划 | 需人拍板 |
|---|---|---|---|---|---|
| 1 | O3 | 伪造票据载荷塞原始 `"query "`：`_parse_ticket_scope` 不识别该串 → 按 G-08 legacy 兼容降级为「无分域票」（迁移期全域放行），不是 fail-closed 拒绝 | 已记账**不修**；主票（`scope="query "` → 200）已于 `f083568` 判「有意设计」收口（`docs/fuzz-report.md:65`），附注见 `:86` | 待 ADR-006 的 legacy 放行窗口**截止日**与「legacy 无 scope」/「非法 scope」的区分口径统一后再说（`docs/adr/README.md:49-52`） | **是** |
| 2 | O4 | `_require_real_redis_allowed()` 在 pytest 进程内**恒放行**（`PYTEST_CURRENT_TEST` 存在），故「Redis 不可达 → 429」不能靠改 env 触发 | 预期行为，未改 `infra/redis.py`（`docs/fuzz-report.md:87`）；已用主动注入 + env 矩阵锁住（`bf4adbf`） | 不改；后续若要真跑「无 Redis」只能继续主动注入（monkeypatch），文档已写明陷阱 | 否 |
| 3 | O5 | 200KB `q` 被 uvicorn/httptools 的 HTTP 解析层 400 拒（请求没进 FastAPI），非应用缺陷 | 框架层拒绝，已收口（`docs/fuzz-report.md:88`） | 仅当业务确需超大 query 时在**网关层**调 `large_client_header_buffers`，不在应用层改 | **是**（仅业务需要时） |
| 4 | W4-1 | `src/hero_quant/agent/context.py:205-208`（**已核验**）：`_collapse(text, 0)` 走 `budget == 0` 分支，`marker[:0]` 返回空串 `''`，与 `:206` 注释「避免空输出」相反 | 只记录未改（`docs/validation-report.md:168`） | 低危卫生项：若要修，把 `:208` 的 `marker[:0]` 改为返回 marker 全文并加 pin；需改 `src/` ⇒ 须派单 | 否（低危，归 P3） |
| 5 | W4-2 | `src/hero_quant/backtest/metrics.py:144-146`（**已核验**，完整段落 `:141-146`）：docstring 两例自相矛盾——`:144-145` 说 `w=[0,1,0]` 单边换手「应为 1.0」，`:146` 又说按日均口径 `w=[0,1,0,1,0]` 得 0.5；实现取日均 | 文档缺陷未改（`docs/validation-report.md:169`） | 改 docstring 示例为日均口径的 0.5（与 `docs/quant-semantics.md` 对齐）；turnover 数值 pin 已在 `6dea1f0` 校准 | 否 |
| 6 | W4-3 | `src/hero_quant/llm/factory.py:199-203`（**已核验**）：`setattr(chat, "timeout", timeout)`（`:201`）在 pydantic v2 模型上被拒（未声明字段），`chat` 缺 `timeout` 内省属性 | `except` 内 debug 日志兜底（`:202-203`，`error_code=llm_chat_timeout_attr_failed`）；超时透传实际由 `LLMClient.timeout` + `_invoke_once` 签名探测保证，**不影响功能** | 三选一：模型声明 `timeout` 字段 / 用 `object.__setattr__` / 直接删掉这段内省属性（无下游依赖）；需改 `src/` ⇒ 须派单 | 否 |
| 7 | W4-4 | `src/hero_quant/backtest/validation.py:53-58`（**已核验**）：`weights_on` / `price_date` / `currency` 的 `kwargs` 兼容分支（`:53/55/57` 三个 `if ... in kwargs`）**不可达**——同名参数已被 Python 绑定到形参，`kwargs` 永远取不到这三个 key | 死代码 3 行，只记录（`docs/validation-report.md:171`；另见 `:183-184`） | 删除该三块（改 `src/`，须派单）；或补一条「`validate(...)` 未知 kwargs fail-closed」的既有 pin 说明其为防御残留 | 否（删代码须派单） |
| 8 | PG 环境受限 | 本机无 PostgreSQL 监听（`[::1]:5432` SYN 无响应），5 个 PG 依赖测试文件共 **44** 例历史上只能 `--deselect`（`docs/validation-report.md:156-161`） | Round 7 X1 线已按 `5a2466b` 改为**守卫 skip**（`tests/conftest.py` +150 行探测 + 5 个文件各加 skip 守卫），不再靠命令行 deselect | Round 7 收尾报告需记一条「口径从 deselect 切到 skip」；CI 侧下一步是提供真 PG service 容器把这些用例跑成 pass | 否（口径变更已在 Round 7 记录） |
| 9 | real 模式评测 | `.env` 的 LLM key 是 `change-me` 占位 → 后端 401 → 正确行为 fail-closed 成 `skipped / no_llm_backend`，**无任何模型质量结论** | 留白不伪造（`docs/agent-eval-real.md:8-13`，`evals/results_real.json`） | 注入可用 provider key 后重跑 `python evals/run.py --mode real`，产出真实通过率 + 失败分类（`docs/agent-eval-real.md:37` T1） | **是**（需凭据 + 计费决策） |
| 10 | Rust / 外网 / 红队 | ① `quantlib/rust.py` 真实 Rust 加速分支本机不可达（未编译 `crates/`，`IS_RUST=False`），只用假扩展驱动语句覆盖（`docs/validation-report.md:177-178`）；② `agent/embed.py` 的 openai / sentence-transformers 真实网络调用未验（本机无外网，`:179-180`）；③ 覆盖率大头 `agent/loop.py` 仍有 **528** 条未覆盖（≈65%，`:175-176`，全是 best-effort 兜底）；④ redline 侧只出 ADR-005/ADR-006 **决策记录**，未落实现（`docs/adr/README.md:44-52`） | 均「本机条件不可达」或「仅文档」，已入台账 | ① CI 编译扩展 + ② CI 装网络后跑真链；③ 需「双故障」注入，ROI 低，暂不动；④ ADR 实施须另立专线（ADR-005 的存量回填一步含 DDL，`migrations/` 冻结） | **是**（① ② ④） |

台账总量核对：O3/O4/O5 三条来自 W2 线（`docs/fuzz-report.md:85-88`），W4-1..W4-4 四条来自 W4 线（`docs/validation-report.md:164-171`），其余 3 条（PG 环境 / real 模式 / Rust-外网-redline）为跨线环境项。3 + 4 + 3 = 10。

## 5. Round 6 后补（`d1715db` 基线归集）

Round 6 收尾时残留 4 个**已 staged 未提交**的改动；Round 7 开工时由总管原样提成基线 commit `d1715db`（内容一字未改，仅归集）。来源为 Round 7 派单口径；下表逐条按 `git show` 的实际 diff 核对后书写。

- 提交元信息：`d1715db` — `chore: 基线归集 Round7 前置（quantlib rust 别名/选列修正 + 2 个 frontend 测试纳入版本 + gitignore 解禁）`
- `git show --stat d1715db` → **4 files changed, 212 insertions(+), 5 deletions(-)**：

| 文件 | 变更 | 内容（按 diff 实际核对） |
|---|---|---|
| `src/hero_quant/quantlib/rust.py` | +18/-5 区域 | ① `bollinger`：Rust 快路径条件从「无 `n`/`k` 别名」改为「`{n,k,std,num_std}` 任一别名存在即回落 Python」——否则 Rust 用默认 `num_std` **静默算错**；② `macd`：兼容 `n_fast`/`n_slow`/`n_signal` 别名（`fast/slow/signal` 未显式给时取别名），使加速路径与 Python 回落口径一致；③ `max_drawdown`：DataFrame 选列与 Python 的 `_to_series` 对齐为 `close → equity → 首列` |
| `frontend/src/__tests__/Live.test.tsx` | **+109** | 原本被 `.gitignore` 忽略的本地测试纳入版本控制 |
| `frontend/src/__tests__/Settings.test.tsx` | **+87** | 同上 |
| `.gitignore` | **-3** | 移除 3 行：注释 `# frontend local tests not yet curated` + 上述两个测试文件的 ignore 行（解禁） |

- 与 Round 6 结论的关系：这三处 `rust.py` 修正属「加速路径与 Python 回落口径一致性」缺陷，`docs/validation-report.md:146` 记的 `quantlib/rust.py` 73%→89%（假扩展驱动）不覆盖真 Rust 别名分支，故本条是**独立于覆盖率口径**的行为修正（对应 §4 台账第 10 条 ①）。

## 6. 交叉引用落点（本次追加的四段）

| 文档 | 追加位置 | 指向 |
|---|---|---|
| [docs/fuzz-report.md](fuzz-report.md) | §4 末尾（`- 工具链注记` 之后） | 本文件 §4 台账总表 + [mutation-audit.md](mutation-audit.md) |
| [docs/mutation-audit.md](mutation-audit.md) | 文末新增 §4 交叉引用 | 本文件 §3 覆盖率口径 + [fuzz-report.md §4](fuzz-report.md) |
| [docs/adr/README.md](adr/README.md) | ADR-006 段之后的 `---` 分隔线下 | 本文件 §4 台账（ADR-005/006 对应台账 O3/O4 两票） |
| [docs/agent-eval-real.md](agent-eval-real.md) | §4 台账表之后 | 本文件 §4 台账第 9 条（real 评测未做项） |

追加原则：只加链接段，不改上述四篇的任何既有结论、数字与排版。

## 7. 核验命令清单（后人可逐条重跑）

```bash
cd /root/projects/hero-quant

# A. 8 条 commit 的标题（逐条核验，期望与本文件 §1 表格逐字一致）
for h in 7b4dae8 67735e8 763ed38 f083568 bf4adbf 78569c1 d556b08 8301ba2; do
  git log --oneline -1 "$h"
  git merge-base --is-ancestor "$h" d1715db && echo "$h in-baseline"
done

# B. W4 规模（31 files / 8303 insertions）
git show --stat d556b08 | tail -3
git show d556b08 --name-only --pretty=format: | grep -c '^tests/'

# C. 覆盖率口径与 44 例对称 deselect
sed -n '126,161p' docs/validation-report.md   # 覆盖率 + 四块目录表 + 环境归一化/44 例
sed -n '164,186p' docs/validation-report.md   # W4-1..4 差异 + W4 遗留台账

# D. 台账第 4-7 项行号回核
sed -n '205,208p' src/hero_quant/agent/context.py     # W4-1：marker[:0] → ''
sed -n '141,146p' src/hero_quant/backtest/metrics.py  # W4-2：docstring 两例
sed -n '199,203p' src/hero_quant/llm/factory.py        # W4-3：setattr timeout
sed -n '53,58p'  src/hero_quant/backtest/validation.py # W4-4：kwargs 兼容三块

# E. 后补基线 d1715db
git show --stat d1715db
git show d1715db -- src/hero_quant/quantlib/rust.py
git show d1715db -- .gitignore

# F. O3/O4/O5 台账原文与 ADR 状态
sed -n '85,88p' docs/fuzz-report.md
sed -n '44,52p' docs/adr/README.md
sed -n '8,13p'  docs/agent-eval-real.md

# G. 本轮唯一真 lint 门 + 全仓历史 lint 计数
.venv/bin/ruff check src          # 期望：All checks passed!
.venv/bin/ruff check . 2>&1 | tail -2   # 期望：Found 413 errors（不在门内）

# H. 本轮改动自检（应只出现 docs/ 下 5 个文件）
git show --stat HEAD
git status --porcelain
```

> 注：§2 的 `pytest -q` / `--cov=src` 两行数字属 Round 6 收尾门禁的转记，重跑需整跑全量套件（本机约 20 分钟），且 Round 7 X1 已改 PG 守卫口径（skip 替代 deselect），复跑结果与 §2 不同属**预期**，以 Round 7 收尾报告为最终口径。
