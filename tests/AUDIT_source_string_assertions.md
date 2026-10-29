# 源码字符串断言审计（read_text 断言源码/文档）

生成日期：2026-10-28（S5 测试门禁线）。范围：tests 下对仓库内 src/、README、docs/、migrations/ 等做 read_text() 后断言文本的位置。

风险定义：断言不验证运行时行为，只验证源码里有没有某段字面量；改文案即假红，写死文案即假绿。
排序：可假绿程度降序（or 兜底 / 枚举恒真 / hasattr 存在性 / 条件分支 / 单断言）。

总计命中：123 个测试函数，134 处 read_text 读取点。

## 第一批已改（本次提交）

| 文件 | 测试函数 | 问题 | 处理 |
|---|---|---|---|
| tests/fuzz/test_o_wall_time.py | test_o_fake_template_unified | 锁旧 600519 长模板（S2 已删），断言恒假/文案耦合 | 改锁两处 _FakeLLM 一致 + 不伪造数字 + 含 query/免责声明 |
| tests/test_docs_honesty.py | test_checkpoint_pg_not_memory | readme 含 PG 即过的 or 恒真 | 锚定 README env 表 HERO_CHECKPOINT_DSN 行 |
| tests/test_docs_honesty.py | test_hash_lock_not_placeholder | requirements-lock 行可能不存在，循环空转 | 先断言存在该行，再检查占位词 |
| tests/test_docs_honesty.py | test_retrieval_eval_has_ablation | 32 在任意位置出现即过 | 锚定 32/128/768 三行表格 + dim 标注 |
| tests/test_ready_degraded.py | test_ready_ok_and_degraded_fields | status_code in (200,503) 恒真 | 改为 status 与状态码自洽：ok=200 / degraded=503 |
| tests/test_ready_degraded.py | test_ready_degraded_when_pg_down | 503 or status==degraded 恒真 | 改为必须 503 且 degraded 且 reasons 含 checkpoint |
| tests/test_ready_degraded.py | test_ready_pg_probe_uses_checkpoint_and_billing | status in (ok,degraded) 恒真 | degraded 必须带 reasons；非 degraded 必须 checkpoint=pg |
| tests/test_api_risk_real.py | test_risk_no_hardcode | !=0.62 or degraded 可用任意值蒙混 | 改为缺 metrics 必 None+reason，有 metrics 原值透传 |
| tests/test_billing.py | test_billing_pg_noop_explicit_warning | 锁已删除的假成功告警文案 | 改为废弃桩返 False + _ensure_available 含 BillingUnavailable |
| tests/test_j8_loop_ckpt_gov.py | test_j8_ledger_tamper_and_rotate | verify() in (True, False) 布尔恒真 | 改为篡改后必须 is False |
| tests/test_j8_loop_ckpt_gov.py | test_j8_ckpt_saver_offline | 两个 in (True, False) 恒真且 dsn=None 会误连 PG | 显式 memory:// + 断言读回写入值 |

## 第二批已改（R8-3，2026-10-29）

处理 28 个测试函数：12 个删除（同文件/他文件已有运行时行为用例，或源码锁已随实现演进失效）、16 个改为运行时断言（并发只初始化一次、caplog 告警、`inspect.signature`/`iscoroutinefunction`、failing commit、`safe_load` 产物、直接断言模块常量）。

**口径修正**：原表 123 个函数中有 46 个读取的是测试产物或配置（`tmp_path`/ledger/trace/metrics.json/`.hierarchy.yaml`），不属于"源码字面量断言"，已在状态列标为"重分类"。真实源码/文档断言为 77 个函数。

**当前计数（AST 实测）**：

| 项 | 数量 |
|---|---|
| 剩余源码/文档 `read_text` 调用点 | 55 |
| 其中：文档/配置/迁移契约（README/CHANGELOG/docs/migrations/.gitignore） | 7 |
| 其中：纯源码结构/死代码回归锁 | 48 |

剩余源码断言 48 个（< 60 目标）。这些保留项的共同理由：负面断言（"某死代码已删除"）与临界区锁无法用单线程行为测试表达；改写反而会降低回归检出能力。

复算命令（在仓库根执行）：

```bash
.venv/bin/python - <<'PY'
import ast, pathlib
root = pathlib.Path('tests')
keys = ('src/','README','CHANGELOG','docs/','migrations','.gitignore')
n = 0
for p in root.rglob('*.py'):
    text = p.read_text(encoding='utf-8')
    tree = ast.parse(text)
    for x in ast.walk(tree):
        if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute) and x.func.attr == 'read_text':
            consts = [c.value for c in ast.walk(x.func.value) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            seg = ast.get_source_segment(text, x) or ''
            if any(k in c for c in consts for k in keys) or '__file__' in seg or 'SRC' in seg:
                n += 1
print(n)
PY
```

## 全量清单（按可假绿程度排序）

| # | 文件:行 | 测试函数 | 读取目标 | 断言（截断） | 风险标签 | R8-3 状态 |
|---|---|---|---|---|---|---|
| 1 | tests/test_0904_b2a.py:79 | test_b2a_akshare_dead_code_removed | src/hero_quant/data/loaders/akshare_loader.py | assert "if not dates" not in src, "死代码 if not dates: 应已删除" | 条件分支、单断言 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 2 | tests/test_retest271_lane_f1.py:45 | test_lane_f_hierarchy_index_tracks_base_fallback | .hierarchy.yaml | assert "base" in blob.lower() or "unknown" in blob.lower() or "fallback" in blob.lower(), \ | or 兜底、单断言 | 重分类（读 hierarchy 产物，非源码） |
| 3 | tests/test_retest271_lane_f2.py:34 | test_lane_f_graph_breaker_per_request | src/hero_quant/agent/graph.py | assert "_breaker" not in src or "breaker=None" in src or "def _leaf_subagent(name, breaker" in src \ | or 兜底、单断言 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 4 | tests/test_retest271_lane_f2.py:121 | test_lane_f_loop_timeout_documents_abandoned | src/hero_quant/agent/loop.py | assert "abandon" in src.lower() or "cooperative" in src.lower() or "deadline" in src.lower(), \ | or 兜底、单断言 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 5 | tests/test_retest271_lane_f3.py:119 | test_lane_f_router_fusion_logs | src/hero_quant/mcp/router.py | assert "logger.warning" in src.split("def router_hybrid_scores")[1].split("def ")[0] or \ | or 兜底、单断言 | 已改（注入 fuse 失败 + caplog） |
| 6 | tests/test_0904_b2a.py:121 | test_b2a_news_copy_dead_removed | src/hero_quant/data/loaders/news.py | assert "copy.copy" not in src, "copy.copy 死代码应删除" | 单断言 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 7 | tests/test_0904_b2a.py:143 | test_b2a_news_snapshot_keys_dead_removed | src/hero_quant/data/loaders/news.py | assert "_SNAPSHOT_KEYS" not in src, "死常量 _SNAPSHOT_KEYS 应删除" | 单断言 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 8 | tests/test_0904_b2a.py:329 | test_b2a_tencent_dead_coerce_removed | src/hero_quant/data/loaders/tencent.py | assert "_coerce_float" not in src, "死代码 _coerce_float 应已删除或重命名为被调用实现" | 单断言 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 9 | tests/test_0904_b3a.py:278 | test_b3a_corr_date_join_misaligned_would_differ | src/hero_quant/tools/correlation.py | assert any(k in src for k in ["inner", "join", "merge", "date", "trade_date"]), "correlation 未引入日期对齐逻辑" | 单断言 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 10 | tests/test_k_g15_defense_lock.py:46 | test_k_g15_constant_time_compare_present | src/hero_quant/api/security.py | assert src.count("compare_digest") >= 2, "HMAC/API-Key 须常量时间比较" | 单断言 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 11 | tests/test_pr3i_audit_polish.py:114 | test_registry_narrowed_still_raises | src/hero_quant/data/registry.py | assert len(bare) < 5, f"裸 except 仍有 {len(bare)} 处，期望 <5" | 单断言 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 12 | tests/test_retest271_lane_d2.py:417 | test_d2_market_no_loader_no_substring | src/hero_quant/tools/market_data.py | assert '"no loader" in str' not in src and "'no loader' in str" not in src | 单断言 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 13 | tests/test_retest271_lane_d2.py:479 | test_d2_quantlib_no_dead_ternary | src/hero_quant/tools/quantlib_tool.py | assert "n if n else 14" not in src | 单断言 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 14 | tests/test_retest271_lane_f1.py:164 | test_lane_f_ingest_no_line_offsets | src/hero_quant/memory/ingest.py | assert "line_offsets" not in src, "dead line_offsets must be removed" | 单断言 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 15 | tests/test_retest271_lane_f1.py:266 | test_lane_f_store_bigram_locked | src/hero_quant/memory/store.py | assert "self._lock" in seg, "_search_bigram_raw must hold self._lock" | 单断言 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 16 | tests/test_retest271_lane_f1.py:277 | test_lane_f_store_write_invalidates_redis | src/hero_quant/memory/store.py | assert "hero:cache:memory:search" in src.split("def clear_retrieval_cache")[1].split("def ")[0], \ | 单断言 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 17 | tests/test_retest271_lane_f3.py:66 | test_lane_f_agent_store_no_dead_keyerror | src/hero_quant/agent/memory/store.py | assert "except KeyError" not in src, "unreachable KeyError handlers must be removed" | 单断言 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 18 | tests/test_retest271_lane_f3.py:129 | test_lane_f_router_embed_logs | src/hero_quant/mcp/router.py | assert "logger" in seg and "except Exception as" in seg | 单断言 | 已改（注入 embed 失败 + caplog） |
| 19 | tests/test_retest271_lane_f3.py:135 | test_lane_f_router_corpus_registry_lock | src/hero_quant/mcp/router.py | assert "_REGISTRY_LOCK" in seg, "corpus build must snapshot under _REGISTRY_LOCK" | 单断言 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 20 | tests/test_w4_agent_trace_io_errors.py:171 | test_write_sidecar_durable_falls_back_to_replace | <expr> | assert target.read_text(encoding="utf-8") == "payload" | 单断言 | 重分类（读测试产物/配置，非源码字面量） |
| 21 | tests/test_0904_c4.py:256 | test_c4_server_backtest_l1_atomic | src/hero_quant/api/server.py | assert w_idx < if_idx, "首检仍在锁外" | or 兜底、条件分支 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 22 | tests/test_docs_honesty.py:41 | test_changelog_has_030_2026_08_28 | CHANGELOG.md | assert "## [0.3.0] - 2026-08-28" in changelog, "CHANGELOG missing 0.3.0 2026-08-28 entry" | or 兜底、枚举恒真 | 保留（文档契约断言） |
| 23 | tests/test_j8_loop_ckpt_gov.py:166 | test_j8_ledger_tamper_and_rotate | <expr> | assert led.verify() in (True, False) | or 兜底、枚举恒真 | 已改（第一批） |
| 24 | tests/test_memory_store_threadsafe.py:19 | test_write_atomic_failure_cleans_file | src/hero_quant/memory/store.py | assert (tmp_path / "k.md").exists(), "k.md should exist after write" | or 兜底、hasattr 存在性 | 已改（PRAGMA WAL + 原子清理） |
| 25 | tests/test_memory_store_threadsafe.py:46 | test_recent_hashes_locked | src/hero_quant/memory/store.py | assert "self._lock" in src, "self._lock not found in store.py" | or 兜底、hasattr 存在性 | 已改（并发写/搜 + RLock + top_k 截断） |
| 26 | tests/test_vector_maturity4.py:131 | test_vector_column_and_cosine_topk_hybrid | src/hero_quant/memory/store.py | assert "vector" in cols or "embedding" in cols or hasattr(ms, "_vector_enabled") | or 兜底、hasattr 存在性 | 已改（去掉源码回退，断言本地模式 + topK 排序） |
| 27 | tests/test_0904_b3a.py:156 | test_b3a_market_shared_registry_has_lock | src/hero_quant/tools/market_data.py | assert hasattr(md, "_shared_lock"), "缺 _shared_lock：并发下重复 _make_registry" | hasattr 存在性 | 已改（8 线程并发只初始化一次） |
| 28 | tests/test_0904_c2.py:225 | test_c2_hierarchy_nonstring_keywords | .hierarchy.yaml | assert "hello" in user_kws or "hello" in [k.lower() for k in user_kws] | or 兜底 | 重分类（读 hierarchy 产物，非源码） |
| 29 | tests/test_0904_c4.py:72 | test_c4_rl_fail_closed_on_redis_error | src/hero_quant/api/rate_limiter.py | assert "return True" not in except_block.group(1), "except 块仍 return True fail-open" | or 兜底 | 删除（同文件 runtime 503 用例已覆盖） |
| 30 | tests/test_0904_c4.py:108 | test_c4_rl_falsy_id_not_anonymous | src/hero_quant/api/rate_limiter.py | assert "is not None" in src, "未用 is not None 显式判定" | 条件分支 | 删除（同文件 runtime falsy id 用例已覆盖） |
| 31 | tests/test_0904_c4.py:140 | test_c4_rl_top_level_import | src/hero_quant/api/rate_limiter.py | assert "RateLimiter" in pre_check, "顶层未导入 RateLimiter" | or 兜底 | 删除（函数内 import 无行为差异） |
| 32 | tests/test_0904_c4.py:177 | test_c4_server_backgroundtasks_no_mutable_default | src/hero_quant/api/server.py | assert "BackgroundTasks([])" not in src, "仍有可变默认 BackgroundTasks([])" | 条件分支 | 已改（inspect.signature 断言默认值） |
| 33 | tests/test_0904_c4.py:210 | test_c4_server_narrow_except_around_to_thread | src/hero_quant/api/server.py | assert "except Exception" not in window or "except RuntimeError" in window, f"to_thread 紧邻宽 except: {window[:300]}" | or 兜底 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 34 | tests/test_0904_c4.py:228 | test_c4_server_shutdown_is_async_and_awaits | src/hero_quant/api/server.py | assert "async def _stop_trace_consumer" in src, "shutdown 未改为 async def" | or 兜底 | 已改（iscoroutinefunction） |
| 35 | tests/test_0904_c4.py:241 | test_c4_server_ready_has_timeout | src/hero_quant/api/server.py | assert src.count("timeout") >= 2, "ready 探活未添加 timeout" | or 兜底 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 36 | tests/test_0904_c4.py:309 | test_c4_router_singleton_double_checked_lock | src/hero_quant/mcp/router.py | assert "with _LIMITER_LOCK" in circ_seg or "with _CORPUS_LOCK" in circ_seg, "_get_router_circuit 未加锁 DCL" | or 兜底 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 37 | tests/test_0904_c4.py:366 | test_c4_router_cache_hit_not_hold_lock_for_cosine | src/hero_quant/mcp/router.py | assert "return _cosine" not in blk, "仍在锁内 return _cosine(query_vec, dvec)" | or 兜底 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 38 | tests/test_agent_graph_schedule.py:7 | test_delegation_depth_increments | src/hero_quant/agent/graph.py | assert "delegation_depth" in src | or 兜底 | 已改（plan_node 运行时断言 Send/update 深度） |
| 39 | tests/test_agent_graph_schedule.py:23 | test_breaker_threadsafe | src/hero_quant/agent/graph.py | assert has_lock, "missing threading.Lock / _breaker_lock for _breaker" | or 兜底 | 删除（锁实现已演进为 _legacy_budget_lock，文本锁失效） |
| 40 | tests/test_agent_loop_core.py:5 | test_keyboard_interrupt_propagates | src/hero_quant/agent/loop.py | assert "(KeyboardInterrupt, SystemExit, GeneratorExit)" in src or "(KeyboardInterrupt,SystemExit,GeneratorExit)" in src, "must handle KeyboardInterrupt/Syste... | or 兜底 | 删除（round7 已有 3 条 KI 透传行为用例） |
| 41 | tests/test_agent_policies.py:44 | test_retry_async_sleep | <expr> | assert has_async and has_asyncio, "RetryPolicy missing async asleep with asyncio.sleep" | or 兜底 | 已改（await asleep 运行时覆盖） |
| 42 | tests/test_agent_prompt_trace.py:52,61 | test_trace_sidecar_full_hash | <expr> | assert trace_file.exists(), "trace.jsonl not created" | or 兜底 | 重分类（读测试产物/配置，非源码字面量） |
| 43 | tests/test_api_server_harden.py:9 | test_request_counter_none_guarded | src/hero_quant/api/server.py | assert has_none_guard, "REQUEST_COUNTER calls must have None guard (if REQUEST_COUNTER is not None)" | or 兜底 | 已改（REQUEST_COUNTER=None 走 /live 与回测端点） |
| 44 | tests/test_ast_guard_wiring.py:257 | test_runner_execute_python_dedup | src/hero_quant/sandbox/runner.py | assert text.count('compile(source, "<sandbox>", "exec")') == 1, "execute_python 重复未消除，应抽取公共 helper" | or 兜底 | 保留（结构/死代码回归锁，行为测试无法表达） |
| 45 | tests/test_bench.py:44 | test_run_batch_regional | <expr> | assert _resolve_benchmark("600519.SS") == "000001.SS" | or 兜底 | 重分类（读测试产物/配置，非源码字面量） |
| 46 | tests/test_docs_honesty.py:27,28 | test_hash_lock_not_placeholder | README.md | assert "--hash=sha256:" in lock, "requirements-lock should contain real hash" | or 兜底 | 已改（第一批） |
| 47 | tests/test_ingest_deterministic.py:50 | test_ingest_key_is_sha256_not_hash | <expr> | assert "hashlib.sha256" in src | or 兜底 | 删除（deterministic key 用例已断言 sha256 后缀） |
| 48 | tests/test_ledger_maturity4.py:29 | test_flock_exclusive_critical_section | <expr> | assert "flock" in src.lower() or "LOCK_EX" in src | or 兜底 | 删除（并发 append 用例已覆盖临界区） |
| 49 | tests/test_ledger_maturity4.py:174 | test_rotate_64mib_constant | <expr> | assert hasattr(ledger, "DEFAULT_ROTATE_BYTES"), "missing DEFAULT_ROTATE_BYTES" | hasattr 存在性 | 已改（直接断言运行时常量值） |
| 50 | tests/test_ledger_maturity4.py:182 | test_dedup_rls_ddl_contains_create_policy | <expr> | assert "CREATE POLICY" in src, "missing CREATE POLICY RLS DDL" | or 兜底 | 已改（断言 DDL_RLS_PG 模块常量） |
| 51 | tests/test_llm_metrics.py:56,62 | test_loop_trace_reason_llm_timeout | <expr> | assert res.reason == "llm_timeout" | or 兜底 | 重分类（读测试产物/配置，非源码字面量） |
| 52 | tests/test_memory_hierarchy_path.py:21 | test_yaml_safe_dump_used | src/hero_quant/memory/hierarchy.py | assert "yaml.safe_dump" in src or "safe_dump" in src | or 兜底 | 已改（safe_load 索引产物并断言内容） |
| 53 | tests/test_news_pit.py:168 | test_bench_run_batch_backward_compatible_and_disclosure | <expr> | assert "AAPL" in metrics | or 兜底 | 重分类（读测试产物/配置，非源码字面量） |
| 54 | tests/test_news_pit.py:199 | test_bench_tearsheet_or_output_contains_non_pit_hint | <expr> | assert "non-pit" in combined or "unavailable" in combined or "disclosure" in combined or "pit" in combined, f"bench ticker metrics missing PIT disclosure hin... | or 兜底 | 重分类（读测试产物/配置，非源码字面量） |
| 55 | tests/test_otel_compose.py:15,19 | <module> | <expr> | assert "hero-quant" in services, "services.hero-quant missing" | or 兜底 | 重分类（读测试产物/配置，非源码字面量） |
| 56 | tests/test_polish_p3.py:61 | test_dashboard_abortsignal | <expr> | assert "AbortController" in txt, "缺 AbortController" | or 兜底 | 重分类（读测试产物/配置，非源码字面量） |
| 57 | tests/test_polish_p3.py:97 | test_registry_narrow_except_samples | <expr> | assert len(narrow) >= 5, f"窄化不足: 仅 {len(narrow)} 处，期望 >=5，剩余记 backlog" | or 兜底 | 重分类（读测试产物/配置，非源码字面量） |
| 58 | tests/test_retest271_lane_c.py:354 | test_c_rotate_does_not_mask_corruption | <expr> | with pytest.raises(lm.LedgerCorruptionError) as ei: | 枚举恒真 | 重分类（读测试产物/配置，非源码字面量） |
| 59 | tests/test_sandbox_maturity4.py:121 | test_landlock_sandbox_enforcement_property | <expr> | assert sb.enforcement in ("unusable", "partial") | 枚举恒真 | 重分类（读测试产物/配置，非源码字面量） |
| 60 | tests/test_trait_whitelist.py:136,138 | test_conftest_autouse_clears_cache | <expr> | assert "clear_settings_cache" in text or "_settings_mode_cache" in text | or 兜底 | 重分类（读测试产物/配置，非源码字面量） |
| 61 | tests/fuzz/test_o_wall_time.py:95 | test_o_fake_template_unified | src/hero_quant/api/server.py | assert len(lits) == 2, f"server 应恰有两处 _FakeLLM 降级模板，实得 {len(lits)}" | - | 已改（第一批） |
| 62 | tests/test_0904_a2b.py:185 | test_b06_append_never_skips_full_verify_on_cache_hit | <expr> | assert str(p) in _tail_verify_cache | - | 重分类（读测试产物/配置，非源码字面量） |
| 63 | tests/test_0904_a3b.py:202 | test_a12_no_duplicate_history_branch | src/hero_quant/security/approval.py | assert "approval/asked" not in src and "approval/decided" not in src | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 64 | tests/test_0904_a3b.py:209 | test_a13_no_unreachable_ask_auto_guard | src/hero_quant/security/approval.py | assert "self.mode == ApprovalPolicy.ASK and folded == ApprovalPolicy.AUTO" not in src | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 65 | tests/test_0904_b1.py:223 | test_b1_bench_no_dead_kwargs | src/hero_quant/backtest/bench.py | assert 'kwargs.pop("dates"' not in src and "kwargs.pop('dates'" not in src | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 66 | tests/test_0904_b2a.py:130 | test_b2a_news_is_aware_hoisted | src/hero_quant/data/loaders/news.py | assert "def _is_aware" in src | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 67 | tests/test_0904_b3a.py:163 | test_b3a_market_get_market_data_reuses_shared_registry | src/hero_quant/tools/market_data.py | assert "_get_shared_registry" in src | - | 已改（桩 registry 断言被调用） |
| 68 | tests/test_0904_b3a.py:181 | test_b3a_market_no_dead_len_probe | src/hero_quant/tools/market_data.py | assert "len(reg) == 0" not in src, "死 len 探测仍存在且必抛 TypeError" | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 69 | tests/test_0904_b3a.py:192 | test_b3a_market_synthetic_no_dead_try | src/hero_quant/tools/market_data.py | assert "# try to produce two bars" not in src | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 70 | tests/test_0904_b3a.py:386 | test_b3a_presentation_holds_rlock_when_iterating | src/hero_quant/tools/presentation.py | assert "_REGISTRY_LOCK" in src, "presentation 未引入 _REGISTRY_LOCK" | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 71 | tests/test_0904_b3b.py:204 | test_b3b_registry_required_check_no_dead_code | src/hero_quant/tools/registry.py | assert "if isinstance(props, dict) and req not in props" not in src | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 72 | tests/test_0904_c2.py:307,323 | test_c2_store_write_db_failure_restore | <expr> | assert f.read_text(encoding="utf-8") == "v1" | - | 重分类（读测试产物/配置，非源码字面量） |
| 73 | tests/test_0904_c4.py:23 | test_c4_rl_shared_bucket_tier_isolation | src/hero_quant/api/rate_limiter.py | assert "endpoint" in src, "rate_limiter 未引入 endpoint 区分" | - | 删除（同文件 runtime tier 隔离用例已覆盖） |
| 74 | tests/test_0904_c4.py:198 | test_c4_server_backgroundtasks_signature | src/hero_quant/api/server.py | assert ( | - | 删除（FastAPI 注入探测用例已覆盖） |
| 75 | tests/test_0904_c4.py:283 | test_c4_server_no_private_registry | src/hero_quant/api/server.py | assert "_names_to_collectors" not in src, "仍依赖私有 _names_to_collectors" | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 76 | tests/test_0904_c4.py:293 | test_c4_router_bm25_snapshot_not_torn | src/hero_quant/mcp/router.py | assert src.count("with _CORPUS_LOCK") >= 2, "BM25 发布未在锁内（with _CORPUS_LOCK 次数不足）" | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 77 | tests/test_0904_c4.py:325 | test_c4_router_circuit_open_preserves_compute_factor | src/hero_quant/mcp/router.py | assert allow_idx != -1 | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 78 | tests/test_0904_c4.py:386 | test_c4_router_vector_except_is_broad | src/hero_quant/mcp/router.py | assert "except (ImportError, ValueError, TypeError)" not in seg, "仍为窄 except 致网络/超时逃逸" | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 79 | tests/test_agent_context_budget.py:17 | test_extra_rules_in_fallback | src/hero_quant/agent/context.py | assert "Extra Rules" in src, "fallback missing Extra Rules marker" | - | 已改（注入 prompt 失败触发 fallback） |
| 80 | tests/test_agent_embed_normalize.py:14 | test_embed_cache_lock | src/hero_quant/agent/embed.py | assert "_CACHE_LOCK" in src, "missing _CACHE_LOCK" | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 81 | tests/test_agent_loop_core.py:63 | test_token_limit_char_conversion | src/hero_quant/agent/loop.py | assert "token_limit" in src and "*4" in src, "token_limit should be converted via *4" | - | 删除（token_limit 数学已由 test_agent_loop 运行时覆盖） |
| 82 | tests/test_agent_prompt_trace.py:24 | test_trace_threshold_validation | src/hero_quant/agent/trace.py | assert "_validate_threshold" in src, "_validate_threshold helper missing in trace.py" | - | 已改（只保留 TraceWriter 行为断言） |
| 83 | tests/test_api_server_harden.py:63 | test_dist_traversal_blocked | src/hero_quant/api/server.py | assert "resolve()" in src, "must use Path.resolve() for dist traversal check" | - | 已改（编码 ../ 请求必须 404） |
| 84 | tests/test_ast_guard_wiring.py:238 | test_sandbox_init_fallback_no_fork | src/hero_quant/sandbox/__init__.py | assert "class SandboxViolation(RuntimeError)" not in text, "fallback forks SandboxViolation identity" | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 85 | tests/test_ast_guard_wiring.py:249 | test_runner_no_dead_bare_reexport | src/hero_quant/sandbox/runner.py | assert "from .ast_guard import SandboxViolation" not in text, "runner 有死的 bare re-export，应改为 alias 委托" | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 86 | tests/test_billing.py:41 | test_ledger_verify_isolation_per_tenant | <expr> | assert ledger.verify(tenant="alice") is True | - | 重分类（读测试产物/配置，非源码字面量） |
| 87 | tests/test_cjk_search.py:67 | test_external_index_searches_existing_summary_without_writing_base_file | <expr> | assert not (tmp_path / "daily__2026-08-22.md.md").exists() | - | 重分类（读测试产物/配置，非源码字面量） |
| 88 | tests/test_docs_honesty.py:6 | test_hero_data_mode_docs_aligns_live | README.md | assert s.data_mode == "live", f"Settings.data_mode should be live, got {s.data_mode}" | - | 保留（文档契约断言） |
| 89 | tests/test_docs_honesty.py:16 | test_checkpoint_pg_not_memory | README.md | assert s.checkpoint_dsn.startswith("postgresql://"), f"checkpoint_dsn should be PG, got {s.checkpoint_dsn}" | - | 已改（第一批） |
| 90 | tests/test_docs_honesty.py:48 | test_retrieval_eval_has_ablation | docs/retrieval_eval.md | assert "ablation" in doc.lower() | - | 已改（第一批） |
| 91 | tests/test_governance_dedup_atomic.py:139 | test_total_changes_bug_fixed | src/hero_quant/governance/dedup.py | assert store.insert_pending(key, "tool") is True | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 92 | tests/test_k_g15_defense_lock.py:17 | test_k_g15_ticket_atomic_single_use | src/hero_quant/api/security.py | assert "getdel" in src and "eval" in src, "票据原子路径（GETDEL+Lua 兜底）不得退化" | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 93 | tests/test_k_g15_defense_lock.py:51,54 | test_k_g15_billing_rls_ddl_present | src/hero_quant/billing/service.py | assert "FORCE ROW LEVEL SECURITY" in svc | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 94 | tests/test_ledger.py:11 | test_ledger_chain | <expr> | assert ledger.verify() is True | - | 重分类（读测试产物/配置，非源码字面量） |
| 95 | tests/test_ledger_maturity4.py:89 | test_verify_full_chain_on_tamper | <expr> | assert ledger.verify() is True | - | 重分类（读测试产物/配置，非源码字面量） |
| 96 | tests/test_ledger_maturity4.py:107 | test_append_refuses_corrupted_chain | <expr> | assert ledger.verify() is False | - | 重分类（读测试产物/配置，非源码字面量） |
| 97 | tests/test_lifecycle_compress.py:87 | test_compress_raw_records_to_daily | <expr> | assert len(actions) == 1 | - | 重分类（读测试产物/配置，非源码字面量） |
| 98 | tests/test_lifecycle_compress.py:110 | test_compress_daily_records_to_digest | <expr> | assert len(actions) == 1 | - | 重分类（读测试产物/配置，非源码字面量） |
| 99 | tests/test_lifecycle_compress.py:130 | test_compress_ignores_empty_and_corrupt_records | <expr> | assert len(actions) == 1 | - | 重分类（读测试产物/配置，非源码字面量） |
| 100 | tests/test_otel_compose.py:117,118 | test_compose_yaml_valid | <expr> | assert isinstance(compose_data, dict) and compose_data, "docker-compose.yml must parse to non-empty dict" | - | 重分类（读测试产物/配置，非源码字面量） |
| 101 | tests/test_polish_p3.py:15 | test_otel_atexit_registered | <expr> | assert "import atexit" in txt, "缺 atexit 导入" | - | 重分类（读测试产物/配置，非源码字面量） |
| 102 | tests/test_polish_p3.py:72,77 | test_research_optimizedeps | <expr> | assert "optimizeDeps" in txt, "vite 未配置 optimizeDeps" | - | 重分类（读测试产物/配置，非源码字面量） |
| 103 | tests/test_pr2e_ledger_incremental.py:47 | test_tail_tamper_detected_on_next_append | <expr> | assert lg.verify() is False | - | 重分类（读测试产物/配置，非源码字面量） |
| 104 | tests/test_pr2e_ledger_incremental.py:70 | test_tail_tamper_same_size_with_forged_mtime_detected | <expr> | assert cached is not None | - | 重分类（读测试产物/配置，非源码字面量） |
| 105 | tests/test_retest271_lane_b.py:356 | test_b_fullwidth_branch_dead_removed | src/hero_quant/security/scanner.py | assert "｜" not in m.group(1) and "＞" not in m.group(1), m.group(1) | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 106 | tests/test_retest271_lane_c.py:329 | test_c_append_mid_prefix_tamper_detected | <expr> | assert len(lines) == 5 | - | 重分类（读测试产物/配置，非源码字面量） |
| 107 | tests/test_retest271_lane_c.py:391 | test_c_nul_break_index_reports_line | <expr> | assert res.ok is False | - | 重分类（读测试产物/配置，非源码字面量） |
| 108 | tests/test_retest271_lane_d2.py:59 | test_d2_bench_no_dead_allow_synthetic_guard | src/hero_quant/backtest/bench.py | assert src.count("requires allow_synthetic=True") <= 1 | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 109 | tests/test_retest271_lane_d2.py:383 | test_d2_corr_synthetic_routing_explicit | src/hero_quant/tools/correlation.py | assert '"synthetic" in msg' not in src and "'synthetic' in msg" not in src | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 110 | tests/test_retest271_lane_d2.py:489 | test_d2_quantlib_sharpe_reuses_coerced | src/hero_quant/tools/quantlib_tool.py | assert "_ = pd.to_numeric" not in src | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 111 | tests/test_retest271_lane_f3.py:103 | test_lane_f_router_no_substring_trigger | src/hero_quant/mcp/router.py | assert "factor" not in toks and "momentum" not in toks | - | 保留（结构/死代码回归锁，行为测试无法表达） |
| 112 | tests/test_round7_loop_graph_replay_branches.py:336 | test_replay_usage_sidecar_written_into_trace_dir | <expr> | assert sidecar.exists() | - | 重分类（读测试产物/配置，非源码字面量） |
| 113 | tests/test_round7_loop_graph_replay_branches.py:426 | test_usage_sidecar_written_next_to_duck_typed_writer | <expr> | assert sidecar.exists() | - | 重分类（读测试产物/配置，非源码字面量） |
| 114 | tests/test_security_stress.py:26 | test_ledger_tamper_detected | <expr> | assert ledger.verify() is True | - | 重分类（读测试产物/配置，非源码字面量） |
| 115 | tests/test_temporal_maturity4.py:74 | test_heartbeat_sidecar_file | <expr> | assert len(fired) >= 2 | - | 重分类（读测试产物/配置，非源码字面量） |
| 116 | tests/test_trace.py:16 | test_trace_atomic_write | <expr> | assert (tmp_path / "trace.jsonl").exists() | - | 重分类（读测试产物/配置，非源码字面量） |
| 117 | tests/test_vcr.py:31,46 | test_llm_usage_recorded_in_trace | <expr> | assert "hello" in result.text, f"result.text should contain hello, got {result.text!r}" | - | 重分类（读测试产物/配置，非源码字面量） |
| 118 | tests/test_vcr.py:103 | test_replay_does_not_call_llm | <expr> | assert "replayed hello" in result.text, f"replayed text missing, got {result.text}" | - | 重分类（读测试产物/配置，非源码字面量） |
| 119 | tests/test_w4_agent_trace_sidecar.py:41 | test_tool_result_non_string_content_is_json_serialized | <expr> | assert "result_path" in recs[0] | - | 重分类（读测试产物/配置，非源码字面量） |
| 120 | tests/test_w4_agent_trace_sidecar.py:83,85 | test_existing_sidecar_with_different_content_gets_random_suffix | <expr> | assert name != f"{digest}.txt" | - | 重分类（读测试产物/配置，非源码字面量） |
| 121 | tests/test_w4_agent_trace_sidecar.py:186 | test_write_sidecar_durable_is_idempotent_when_target_exists | <expr> | assert w._write_sidecar_durable(target, "new-content") is None | - | 重分类（读测试产物/配置，非源码字面量） |
| 122 | tests/test_w4_agent_trace_sidecar.py:197 | test_write_sidecar_durable_creates_file | <expr> | assert target.read_text(encoding="utf-8") == "payload-123" | - | 重分类（读测试产物/配置，非源码字面量） |
| 123 | tests/test_w4_backtest_bench_branches.py:204 | test_run_batch_json_output_path_writes_that_file | <expr> | assert target.exists() | - | 重分类（读测试产物/配置，非源码字面量） |
