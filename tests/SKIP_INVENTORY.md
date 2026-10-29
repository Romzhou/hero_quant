# 测试 skip 清单（2026-10-29，R8-3）

运行命令：`.venv/bin/python -m pytest -q -rs`
结果：**2761 passed / 54 skipped**（未导出 PG DSN、worktree 无 `frontend/dist`）。

| 类别 | 数量 | 说明 | 何时消失 |
|---|---|---|---|
| PG 守卫（`PG guard: PostgreSQL 不可达`） | 47 | 未导出 `HERO_CHECKPOINT_DSN`/`HERO_PG_DSN`，PG 集成族整族 skip | 配好 PG 后归零；CI 已用 postgres service 提供，并加 "no PG guard skips" 断言 |
| RLS 实证（`HERO_RLS_TEST_DSN` 未配置） | 3 | 需要非 superuser 角色 `heroquant_app` 真连 PG，默认 CI 不提供 | 显式导出 `HERO_RLS_TEST_DSN` 后真跑 |
| 真实 Redis DSN（fuzz 门禁用例） | 2 | 环境里配置了真实 `HERO_REDIS_DSN`，与 fakeredis 伪造载荷语义不同 | 不配真实 Redis（CI 默认）时真跑 |
| 无 `frontend/dist` | 1 | `test_frontend_spa.py` 的静态资源用例；worktree 未构建前端 | `npm run build` 后真跑 |
| Windows 语义 | 1 | 盘符冒号仅在 Windows 下非法，POSIX 上合法 | 平台相关，永久保留 |

## PG 守卫分布（47）

| 文件 | 数量 |
|---|---|
| tests/test_0904_c1.py | 15 |
| tests/test_checkpoint_pg.py | 13 |
| tests/test_j8_loop_ckpt_gov.py | 10 |
| tests/test_vector_maturity4.py | 5 |
| tests/test_retrieval_eval.py | 2 |
| tests/test_billing_persistence.py | 2 |

## 结论

- 默认本地跑（不配 PG）会看到 47 + 3 个 PG 相关 skip；其中 47 个是"环境缺失"，不是"功能未实现"。
- CI 已提供 postgres service 与 `HERO_CHECKPOINT_DSN`，并新增步骤断言日志中不得出现 `PG guard`，防止这 47 个用例再次隐身。
- 3 个 RLS 用例按设计仅在本机显式提供 `HERO_RLS_TEST_DSN` 时运行（见 `docs/plan-2026-10-28-round8.md` D-2）。
