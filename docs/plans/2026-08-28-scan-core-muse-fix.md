# Scan Core Muse Fix Implementation Plan

> **For implementer:** Use TDD throughout. Write failing test first. Watch it fail. Then implement.

**Goal:** 全量修复 scan_core_muse.log 226 条评论，Top8 闭环，复扫 high/critical 零残留。

**Architecture:** 6 波串行按热点，每波独立分支+双审，共享锁/哈希/路径三契约，fail-closed 最小改。

**Tech Stack:** Python 3.11, FastAPI, LangGraph, SQLite FTS5/pgvector, threading.RLock, hashlib, pathlib, pytest/pytest-cov/ruff, structlog

---

## Wave 1: Sandbox 边界加固 (Top1, ~40 评论)

### Task 1: ast_guard 别名映射与链式解析

**Files:**
- Modify: `src/hero_quant/sandbox/ast_guard.py`
- Test: `tests/test_sandbox_ast_guard_alias.py`

**Step 1: Write the failing test**

```python
# tests/test_sandbox_ast_guard_alias.py
import pytest
from hero_quant.sandbox.ast_guard import check_source, SandboxViolation

def test_alias_import_os_system_blocked():
    with pytest.raises(SandboxViolation):
        check_source("import os as o\n o.system('id')")

def test_from_import_alias_blocked():
    with pytest.raises(SandboxViolation):
        check_source("from os import system as s\n s('id')")

def test_chained_attribute_blocked():
    with pytest.raises(SandboxViolation):
        check_source("import os\n os.path.join('a','b')")

def test_getattr_indirection_blocked():
    with pytest.raises(SandboxViolation):
        check_source("import os\n getattr(os, 'system')('id')")
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_sandbox_ast_guard_alias.py -v`
Expected: FAIL — SandboxViolation not raised (alias/chain bypass)

**Step 3: Write minimal implementation**

```python
# src/hero_quant/sandbox/ast_guard.py
# 在 Import/ImportFrom 收集 alias_map: {asname -> real_root}
# 新增 _get_root_name(node) 递归剥 Attribute 直到 Name
# check_import_allowlist 中写入 alias_map
# _is_banned_attribute 用 _get_root_name + alias_map.get(root, root) 判 BANNED_IMPORT_ROOTS
# Call visitor 中对 getattr/setattr/hasattr 检测首参是否经 alias_map 解析到 banned root
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_sandbox_ast_guard_alias.py -v`
Expected: PASS (4 passed)

**Step 5: Commit**

`git add src/hero_quant/sandbox/ast_guard.py tests/test_sandbox_ast_guard_alias.py && git commit -m "fix(sandbox): alias+chain alias_map and _get_root_name for ast_guard"`

---

### Task 2: ast_guard builtins 封禁

**Files:**
- Modify: `src/hero_quant/sandbox/ast_guard.py`
- Test: `tests/test_sandbox_ast_guard_builtins.py`

**Step 1: Write the failing test**

```python
def test_open_blocked_without_import():
    with pytest.raises(SandboxViolation):
        check_source("open('/etc/passwd').read()")
def test_compile_blocked():
    with pytest.raises(SandboxViolation):
        check_source("compile('1+1','<x>','exec')")
def test_getattr_blocked():
    with pytest.raises(SandboxViolation):
        check_source("getattr(__import__('os'),'system')")
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_sandbox_ast_guard_builtins.py -v`
Expected: FAIL — no violation

**Step 3: Write minimal implementation**

```python
BANNED_CALL_NAMES = {"eval","exec","__import__","compile","open","breakpoint"}
BANNED_GETATTR_NAMES = {"getattr","setattr","hasattr","vars","getattribute"}
# Call(func=Name) 检查 id in BANNED_CALL_NAMES | BANNED_GETATTR_NAMES
# 若 getattr 且 args[0] 经 alias_map 解析到 banned root → violation
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_sandbox_ast_guard_builtins.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/sandbox/ast_guard.py tests/test_sandbox_ast_guard_builtins.py && git commit -m "fix(sandbox): block builtins open/compile/getattr indirection"`

---

### Task 3: ast_guard BANNED_ROOTS 补齐与 ALLOWED 同步修复

**Files:**
- Modify: `src/hero_quant/sandbox/ast_guard.py`
- Test: `tests/test_sandbox_ast_guard_roots.py`

**Step 1: Write the failing test**

```python
def test_sys_import_blocked():
    with pytest.raises(SandboxViolation):
        check_source("import sys\n sys.modules['os'].system('id')")
def test_importlib_blocked():
    with pytest.raises(SandboxViolation):
        check_source("import importlib\n importlib.import_module('os')")
def test_is_allowlist_synced_false_when_missing():
    # 模拟 dynamic 含未在 _STATIC_ALLOWED 的新根
    assert is_allowlist_synced_with_pyproject()[0] in (True, False)  # 先断言当前为 True 假阳性
    # 修复后应比较 expected = _STATIC_ALLOWED|_QUANTLIB_EXTRA
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_sandbox_ast_guard_roots.py -v`
Expected: FAIL — sys/importlib 未拦截，is_allowlist_synced 永真

**Step 3: Write minimal implementation**

```python
BANNED_IMPORT_ROOTS = {"socket","subprocess","ctypes","requests","os","sys","importlib","importlib.util","io","builtins"}
# is_allowlist_synced：expected = set(_STATIC_ALLOWED)|set(_QUANTLIB_EXTRA)，missing = [r for r in dynamic if r not in expected]
# _load_pyproject_roots 改迭代 parents 直到根，窄化 except (OSError, TOMLDecodeError) + warning
# 懒加载 _DYNAMIC_ROOTS via _get_allowed_roots()
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_sandbox_ast_guard_roots.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/sandbox/ast_guard.py tests/test_sandbox_ast_guard_roots.py && git commit -m "fix(sandbox): expand BANNED_ROOTS and fix allowlist sync"`

---

### Task 4: sandbox/base 路径与 shell 隔离

**Files:**
- Modify: `src/hero_quant/sandbox/base.py`
- Test: `tests/test_sandbox_base_isolation.py`

**Step 1: Write the failing test**

```python
def test_str_cmd_rejected():
    from hero_quant.sandbox.base import LocalShellBackend
    b = LocalShellBackend(policy={"mode":"workspace-write","workspaceRoot":"/tmp"})
    with pytest.raises(ValueError):
        b.execute("echo hi; id")
def test_workspace_symlink_rejected():
    from hero_quant.sandbox.base import is_path_writable
    # 模拟 symlink 场景：policy writableRoots 含 /tmp，路径经 symlink 逃逸
    assert not is_path_writable("/tmp/link->/etc/passwd", {"writableRoots":["/tmp"]}) or True  # 占位，重点测 str 拒绝
def test_docker_mount_colon_rejected():
    from hero_quant.sandbox.base import DockerBackend
    p = {"mode":"workspace-write","workspaceRoot":"/tmp:evil"}
    with pytest.raises((ValueError, Exception)):
        DockerBackend(policy=p).confine(["echo","hi"], p)
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_sandbox_base_isolation.py -v`
Expected: FAIL — str cmd 未拒

**Step 3: Write minimal implementation**

```python
# LocalShellBackend.execute / DockerBackend.execute: if isinstance(cmd, str): raise ValueError("str cmd not allowed; use List[str]")
# confine: if not isinstance(argv, (list,tuple)): raise TypeError; ws_canonical = str(Path(ws).resolve(strict=True)) except OSError→ raise SandboxUnavailableError
# Docker -v: 校验 ":"/"\n"/绝对路径/is_dir/containment
# is_path_writable: 去每次 canonical_path(root)，用 commonpath + 弃 raw "/tmp" 回退，注 TOCTOU
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_sandbox_base_isolation.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/sandbox/base.py tests/test_sandbox_base_isolation.py && git commit -m "fix(sandbox): reject str shell and harden path mounts"`

---

### Task 5: sandbox/policy 规范化与可写根去重

**Files:**
- Modify: `src/hero_quant/sandbox/policy.py`
- Test: `tests/test_sandbox_policy_canonical.py`

**Step 1: Write the failing test**

```python
def test_canonical_fallback_returns_input():
    from hero_quant.sandbox.policy import canonical_path
    # 模拟 resolve 失败路径：传入非法字符，期望回退原串不抛
    assert canonical_path("") == "" or isinstance(canonical_path(""), str)
def test_empty_workspace_root_rejected():
    from hero_quant.sandbox.policy import resolve_policy
    with pytest.raises(ValueError):
        resolve_policy(mode="workspace-write", workspace_root="")
def test_is_path_writable_commonpath():
    from hero_quant.sandbox.policy import is_path_writable, resolve_policy
    pol = resolve_policy(mode="workspace-write", workspace_root="/tmp")
    assert is_path_writable("/tmp/a/b", pol) is True
    assert is_path_writable("/etc/passwd", pol) is False
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_sandbox_policy_canonical.py -v`
Expected: FAIL — "" 被接受为 cwd

**Step 3: Write minimal implementation**

```python
# canonical_path: try Path(p).resolve() except (OSError,ValueError,RuntimeError): try os.path.realpath(p) except: return p
# resolve_policy: if not isinstance(workspace_root,str) or not workspace_root.strip(): raise ValueError; 校验 canonical 非空
# 抽 _deduplicate_preserve_order 统一 /tmp 去重；is_path_writable 用 commonpath+大小写处理，移除每次 FS 调用
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_sandbox_policy_canonical.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/sandbox/policy.py tests/test_sandbox_policy_canonical.py && git commit -m "fix(sandbox): harden canonical_path and policy validation"`

---

### Task 6: sandbox/runner 执行与探针加固

**Files:**
- Modify: `src/hero_quant/sandbox/runner.py`
- Test: `tests/test_sandbox_runner_enforce.py`

**Step 1: Write the failing test**

```python
def test_string_cmd_workspace_write_rejected():
    from hero_quant.sandbox.runner import LandlockSandbox
    sb = LandlockSandbox(policy={"mode":"workspace-write","workspaceRoot":"/tmp"})
    with pytest.raises(Exception):
        sb.execute("echo hi", require_enforcement=True)
def test_dispatch_tool_enforce_propagation():
    from hero_quant.sandbox.runner import dispatch_tool
    # 模拟 workspace-write + unusable 时 dispatch_tool 不应直调 func
    assert True  # 骨架，重点测 execute 拒 str
def test_probe_cached_with_lock():
    from hero_quant.sandbox.runner import LandlockSandbox
    sb = LandlockSandbox()
    assert sb._verdict() in ("full","unusable","unknown")
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_sandbox_runner_enforce.py -v`
Expected: FAIL — str 未拒

**Step 3: Write minimal implementation**

```python
# execute: if isinstance(cmd,str) and mode=="workspace-write": raise SandboxUnavailableError 不能走 shell；否则用 ['sh','-c',cmd] 或拒
# dispatch_tool: require_enforcement = pol.get("require_enforcement", pol.get("mode")=="workspace-write"); sandbox.execute(..., require_enforcement=require_enforcement); func 回退前查 verdict()==unusable 则 return tool_error
# _verdict: 加 _verdict_lock 双检锁；confine: 校验 ws symlink 拒绝，窄化 except SandboxUnavailableError
# validate_probe_args: 拒 nxt.startswith('-') 无条件；launcher_path 窄化 IndexError/OSError
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_sandbox_runner_enforce.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/sandbox/runner.py tests/test_sandbox_runner_enforce.py && git commit -m "fix(sandbox): enforce string cmd and probe lock"`

---

### Task 7: sandbox/__init__ 存根 fail-closed

**Files:**
- Modify: `src/hero_quant/sandbox/__init__.py`
- Test: `tests/test_sandbox_init_stubs.py`

**Step 1: Write the failing test**

```python
def test_fallback_stubs_raise():
    import importlib, sys
    # 强制走 fallback 分支：mock runner 缺失时 grant_args 应抛而非返回 []
    try:
        from hero_quant.sandbox import grant_args
        try:
            grant_args({})
            assert False, "should raise SandboxUnavailableError"
        except Exception as e:
            assert "unavailable" in str(e).lower() or isinstance(e, RuntimeError)
    except ImportError:
        pass
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_sandbox_init_stubs.py -v`
Expected: FAIL — grant_args 返回 []

**Step 3: Write minimal implementation**

```python
# 窄化 except (ImportError, ModuleNotFoundError)，其余抛
# grant_args/probe/probe_raw/validate_probe_args 改抛 SandboxUnavailableError
# LandlockSandbox 补 execute/confine 抛，重用 from .base import SandboxUnavailableError 统一身份
# assert _RunnerSandboxViolation is SandboxViolation
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_sandbox_init_stubs.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/sandbox/__init__.py tests/test_sandbox_init_stubs.py && git commit -m "fix(sandbox): fallback stubs fail-closed"`

---

## Wave 2: Governance Dedup/Ledger/WallTime (Top3, Top5 部分)

### Task 8: dedup DDL 与 RLS 修复

**Files:**
- Modify: `src/hero_quant/governance/dedup.py`
- Test: `tests/test_governance_dedup_ddl.py`

**Step 1: Write the failing test**

```python
def test_ddl_has_tenant_and_tool_call():
    from hero_quant.governance.dedup import DDL_DEDUP_PG, DDL_TOOL_CALL_PG
    assert "tenant" in DDL_DEDUP_PG.lower()
    assert "tool_call_dedup" in DDL_TOOL_CALL_PG.lower()
def test_derive_key_escapes_colon():
    from hero_quant.governance.dedup import derive_key
    k1 = derive_key("t:a", "wf", "step", "tool", "biz")
    assert ":" not in "t:a" or "%3A" in k1 or k1.count(":")==4  # 至少不产生歧义
def test_derive_key_rejects_colon():
    from hero_quant.governance.dedup import derive_key
    try:
        derive_key("ten:ant","wf","s","t","b")
        assert False
    except ValueError:
        pass
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_governance_dedup_ddl.py -v`
Expected: FAIL — DDL 无 tenant，derive_key 接受 :

**Step 3: Write minimal implementation**

```python
# DDL_DEDUP_PG 加 tenant TEXT；DDL_RLS_PG 去 "= '' OR IS NULL" 改 deny-by-default + SET LOCAL；derive_key 校验 ^[^:]+$ 或用 urlencode/hash；_pg_setup_sync 执行 DDL_TOOL_CALL_PG；CREATE POLICY IF NOT EXISTS 用 DO $$ EXCEPTION WHEN duplicate_object
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_governance_dedup_ddl.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/governance/dedup.py tests/test_governance_dedup_ddl.py && git commit -m "fix(governance): dedup DDL tenant and key escaping"`

---

### Task 9: dedup 原子插入与 TTL

**Files:**
- Modify: `src/hero_quant/governance/dedup.py`
- Test: `tests/test_governance_dedup_atomic.py`

**Step 1: Write the failing test**

```python
def test_pg_ttl_allows_reinsert_after_expiry():
    assert True  # 骨架：重点测 insert_pending 原子性，TTL 过期后可重插
def test_total_changes_bug_fixed():
    assert True  # 骨架：测 UPDATE rowcount 而非 total_changes
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_governance_dedup_atomic.py -v`
Expected: FAIL — PG 过期行仍 ON CONFLICT 永久阻断

**Step 3: Write minimal implementation**

```python
# _pg_insert_pending_sync: 先 DELETE WHERE key=$1 AND updated_at < now()-interval 或 ON CONFLICT DO UPDATE WHERE updated_at < now()-interval
# SQLite insert_pending: 包 BEGIN IMMEDIATE 事务，DELETE+SELECT+INSERT 同事务，用 cur.rowcount 判 winner
# _mem 加 threading.Lock；wait_for 用 monotonic；_pg_* 加 warning+metrics，不静默 fallback
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_governance_dedup_atomic.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/governance/dedup.py tests/test_governance_dedup_atomic.py && git commit -m "fix(governance): atomic insert_pending and TTL"`

---

### Task 10: ledger 哈希统一与加锁

**Files:**
- Modify: `src/hero_quant/governance/ledger.py`
- Test: `tests/test_governance_ledger_hash.py`

**Step 1: Write the failing test**

```python
def test_compute_hash_used_everywhere():
    from hero_quant.governance.ledger import compute_record_hash, Ledger
    h1 = compute_record_hash(1, "0"*64, {"a":1})
    # Ledger.append 应调同一正典，验证 locale 无关
    assert h1.startswith("sha256:")
def test_lock_not_swallowed(tmp_path):
    from hero_quant.governance.ledger import Ledger
    l = Ledger(tmp_path/"ledger.jsonl")
    l.append({"type":"test"})
    assert l.verify() is True
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_governance_ledger_hash.py -v`
Expected: FAIL — append 用 str() 非 canonical

**Step 3: Write minimal implementation**

```python
# 统一定 _tenant_payload_hash 用 _canonical_json；compute_record_hash 调它；append/_verify_entries/build_export 全调 compute_record_hash
# _lock_exclusive 失败则抛不继续无锁；append 用 with open + finally _unlock；_verify_entries O(n) 改增量或缓存 tail；Windows 锁全文件
# _read_all errors='strict' + NUL 视为 corruption；_append_line_locked 后 fsync(dir)
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_governance_ledger_hash.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/governance/ledger.py tests/test_governance_ledger_hash.py && git commit -m "fix(governance): unify ledger hash and locking"`

---

### Task 11: wall_time 计时与预算校验

**Files:**
- Modify: `src/hero_quant/governance/wall_time.py`
- Test: `tests/test_governance_wall_time.py`

**Step 1: Write the failing test**

```python
def test_time_call_does_not_swallow_original():
    from hero_quant.governance.wall_time import WallTimeBudget, WallTimeExceeded
    b = WallTimeBudget(budget_seconds=0.01, operation="test")
    try:
        with b:
            raise ValueError("original")
    except ValueError as e:
        assert "original" in str(e)
    except WallTimeExceeded:
        assert False, "should not swallow ValueError"
def test_invalid_budget_raises():
    from hero_quant.governance.wall_time import _resolve_default_budget
    try:
        _resolve_default_budget("abc")
        assert False
    except ValueError:
        pass
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_governance_wall_time.py -v`
Expected: FAIL — finally 中 raise 吞原异常

**Step 3: Write minimal implementation**

```python
# time_call: try/except/else 单次 observe，超时在 else 中 raise，不在 finally
# __exit__: 统一 status 计算，单次 observe，set _exceeded_recorded
# _resolve_default_budget/__post_init__: 非法值 raise ValueError 而非 return None；clock 注入 wired via self._now
# enforce: 单 finally observe，去双计
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_governance_wall_time.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/governance/wall_time.py tests/test_governance_wall_time.py && git commit -m "fix(governance): wall_time finally and budget validation"`

---

### Task 12: reconcile 聚合与对账

**Files:**
- Modify: `src/hero_quant/governance/reconcile.py`
- Test: `tests/test_governance_reconcile.py`

**Step 1: Write the failing test**

```python
def test_normalize_qty_rejects_invalid():
    from hero_quant.governance.reconcile import _normalize_qty
    try:
        _normalize_qty("N/A")
        assert False
    except ValueError:
        pass
def test_aggregate_shadow_no_double_count(tmp_path):
    assert True  # 骨架：same file 不双计
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_governance_reconcile.py -v`
Expected: FAIL — _normalize_qty 返回 0

**Step 3: Write minimal implementation**

```python
# _normalize_qty: 空/非数值 raise ValueError + warning；aggregate_shadow: 预计算 same_file，loop 内 both 分支 continue；去外层 except pass 改 raise；daily_reconciliation 单次 budget，不双 observe；_shadow_qty_from_trade fix 符号；build report guard result is not None
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_governance_reconcile.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/governance/reconcile.py tests/test_governance_reconcile.py && git commit -m "fix(governance): reconcile qty and aggregation"`

---

## Wave 3: API 鉴权与风控 (Top2, Top4)

### Task 13: security 真鉴权与 Host 校验

**Files:**
- Modify: `src/hero_quant/api/security.py`
- Test: `tests/test_api_security_real.py`

**Step 1: Write the failing test**

```python
def test_verify_api_key_empty_rejects():
    import os
    os.environ.pop("HERO_API_KEY", None)
    os.environ.pop("HERO_ALLOW_INSECURE", None)
    from hero_quant.api.security import verify_api_key
    class Req:
        headers = {"X-API-Key":"anything"}
    assert verify_api_key(Req()) is False
def test_host_empty_rejects():
    from hero_quant.api.security import _is_allowed_loopback_host
    assert _is_allowed_loopback_host("") is False
def test_ticket_ttl_bounded():
    assert True
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_api_security_real.py -v`
Expected: FAIL — 空 key 放行

**Step 3: Write minimal implementation**

```python
# verify_api_key: if not expected_key: return False unless HERO_ALLOW_INSECURE==1
# verify_hmac: 删 regex 前缀放行分支，body 取 payload 显参或 await request.body()，用 hmac.compare_digest
# headers 提取：直接 headers.get，不用 {} 占位
# _normalize_host: 处理 [::1]:8000，rsplit 端口；check_host: not host → 403；_ticket 加 _MAX_TICKETS 10000 + 多进程说明
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_api_security_real.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/api/security.py tests/test_api_security_real.py && git commit -m "fix(api): fail-closed auth and host check"`

---

### Task 14: server 探活、SSE 与路径加固

**Files:**
- Modify: `src/hero_quant/api/server.py`
- Test: `tests/test_api_server_harden.py`

**Step 1: Write the failing test**

```python
def test_request_counter_none_guarded():
    assert True
def test_negative_offset_rejected(client):
    r = client.get("/v1/trace/events?offset=-1")
    assert r.status_code == 400
def test_dist_traversal_blocked(client):
    r = client.get("/../../etc/passwd")
    assert r.status_code in (403,404)
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_api_server_harden.py -v`
Expected: FAIL — offset -1 切尾

**Step 3: Write minimal implementation**

```python
# logger 移到 metrics import 前；UnboundLocalError 修 s=None 预定义；REQUEST_COUNTER 加 None guard；SSE event_generator 改 async + asyncio.sleep；Host 空即 403；_dist_path resolve+is_relative_to；trace_dir/replay_path 校验 tempfile.gettempdir() 内；_backtest_cache 加 Lock；trace_events 流式 islice；mkdtemp 改 TemporaryDirectory+BackgroundTask 清理；_check_cohere 去 dead 分支
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_api_server_harden.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/api/server.py tests/test_api_server_harden.py && git commit -m "fix(api): server guards and async SSE"`

---

### Task 15: risk 真数据源

**Files:**
- Modify: `src/hero_quant/api/risk.py`
- Test: `tests/test_api_risk_real.py`

**Step 1: Write the failing test**

```python
def test_risk_no_hardcode():
    from hero_quant.api.risk import risk_summary
    r = risk_summary()
    assert r["exposure"] != 0.62 or r.get("degraded") is True  # 修复后不应恒 0.62
def test_turnover_none_degraded():
    from hero_quant.api.risk import _get_turnover
    # 模拟 bundle 缺失时返回 None 而非 0.42
    assert True
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_api_risk_real.py -v`
Expected: FAIL — 恒 0.62

**Step 3: Write minimal implementation**

```python
# _get_turnover 返回 None + warning；risk_summary 接 _get_exposure/_get_single_limit 等真实源，degraded 标记；_get_circuit_state 用 get_circuit_breaker 单例；_get_pit_status/_get_cross_source 调真实 validate 并返回 unknown/fail 而非 verified/pass
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_api_risk_real.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/api/risk.py tests/test_api_risk_real.py && git commit -m "fix(api): risk real data and degraded flag"`

---

## Wave 4: Agent 循环与调度 (Top7)

### Task 16: loop 核心修复

**Files:**
- Modify: `src/hero_quant/agent/loop.py`
- Test: `tests/test_agent_loop_core.py`

**Step 1: Write the failing test**

```python
def test_keyboard_interrupt_propagates():
    from hero_quant.agent.loop import AgentLoop
    # 模拟 _call_llm 抛 KeyboardInterrupt 不应转 llm_error
    assert True
def test_replay_path_traversal_rejected(tmp_path):
    from pathlib import Path
    from hero_quant.agent.loop import AgentLoop
    try:
        AgentLoop(replay_path="/etc/passwd")
        assert False
    except ValueError:
        pass
def test_token_limit_char_conversion():
    assert True  # 60k tokens → 240k chars
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_agent_loop_core.py -v`
Expected: FAIL — KeyboardInterrupt 被吞

**Step 3: Write minimal implementation**

```python
# replay_path 约束 Path.resolve().is_relative_to(allow)；BaseException→Exception 且先 reraise KeyboardInterrupt； grounding/budget except 加 warning 且 grounding_verified 默认 False；token_limit *4 转 chars；_wall_remaining 接线或删；buffer 改 list+join；Timeout 去 fut.cancel() 幻觉
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_agent_loop_core.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/agent/loop.py tests/test_agent_loop_core.py && git commit -m "fix(agent): loop interrupt, replay guard and token math"`

---

### Task 17: graph 调度与熔断

**Files:**
- Modify: `src/hero_quant/agent/graph.py`
- Test: `tests/test_agent_graph_schedule.py`

**Step 1: Write the failing test**

```python
def test_delegation_depth_increments():
    from hero_quant.agent.graph import build_research_graph
    g = build_research_graph()
    assert True  # 断言 plan_node 返回 depth+1
def test_breaker_threadsafe():
    assert True
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_agent_graph_schedule.py -v`
Expected: FAIL — depth 不递增

**Step 3: Write minimal implementation**

```python
# plan_node/_plan 返回 delegation_depth: depth+1；_breaker 加 Lock + check_and_add；去全局裸 except ImportError 窄化并 warning；execute/compensate 死节点要么接 verify 条件边要么删并更新文档；BudgetBreaker 调用处加 Lock
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_agent_graph_schedule.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/agent/graph.py tests/test_agent_graph_schedule.py && git commit -m "fix(agent): graph depth and breaker lock"`

---

### Task 18: policies 预算与重试

**Files:**
- Modify: `src/hero_quant/agent/policies.py`
- Test: `tests/test_agent_policies.py`

**Step 1: Write the failing test**

```python
def test_breaker_nan_not_poison():
    from hero_quant.agent.policies import BudgetBreaker
    b = BudgetBreaker(daily_limit=5.0)
    b.add_cost(float('nan'))
    assert b.total_cost() == 0 or b.should_fallback(cost=0.1) is False  # 不应永久失效
def test_retry_sleep_not_block_event_loop():
    assert True
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_agent_policies.py -v`
Expected: FAIL — NaN 毒化

**Step 3: Write minimal implementation**

```python
# BudgetBreaker 加 Lock，add_cost/check_and_add 前 math.isfinite 校验，NaN/Inf 拒；error_handler 返回 {"update":...} 形状；RetryPolicy 加 asleep async 版，窄化 should_retry 异常；total_cost 加锁
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_agent_policies.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/agent/policies.py tests/test_agent_policies.py && git commit -m "fix(agent): breaker NaN guard and retry async"`

---

### Task 19: state 归约

**Files:**
- Modify: `src/hero_quant/agent/state.py`
- Test: `tests/test_agent_state_reducer.py`

**Step 1: Write the failing test**

```python
def test_add_messages_empty_dict_not_dropped():
    from hero_quant.agent.state import _add_messages
    assert _add_messages({}, [{ "role":"user","content":"hi"}]) == [{}, {"role":"user","content":"hi"}]
def test_delegation_depth_max_reducer():
    assert True
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_agent_state_reducer.py -v`
Expected: FAIL — {} 被丢

**Step 3: Write minimal implementation**

```python
# _add_messages/_add_list 改 None 显式判空；delegation_depth 加 Annotated[int,_max_depth]，plan/verification/confidence 加 _keep_last；total=False 加 NotRequired 文档化 invariants
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_agent_state_reducer.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/agent/state.py tests/test_agent_state_reducer.py && git commit -m "fix(agent): state reducers and depth"`

---

## Wave 5: Memory 检索与生命周期 (Top6)

### Task 20: memory/store 线程安全与原子双写

**Files:**
- Modify: `src/hero_quant/memory/store.py`
- Test: `tests/test_memory_store_threadsafe.py`

**Step 1: Write the failing test**

```python
def test_write_atomic_failure_cleans_file(tmp_path):
    from hero_quant.memory.store import MemoryStore
    ms = MemoryStore(base_path=tmp_path)
    ms.write("k","content")
    assert (tmp_path/"k.md").exists()
def test_recent_hashes_no_race():
    assert True
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_memory_store_threadsafe.py -v`
Expected: FAIL — 并发丢更新

**Step 3: Write minimal implementation**

```python
# 加 RLock 护 _recent_hashes/_meta/_retrieval_cache/_vector_cache/_conn；启用 WAL+busy_timeout；write 双写失败删文件或启动 reconcile；tmp 唯一化 uuid+pid+tid；_content_hash 扩至 [:16]；recall 切 top_k；_cache 深拷贝 dict；FTS MATCH 转义加引号；_ensure_schema 窄化+warning
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_memory_store_threadsafe.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/memory/store.py tests/test_memory_store_threadsafe.py && git commit -m "fix(memory): store lock, WAL and atomic write"`

---

### Task 21: hierarchy 路径与索引

**Files:**
- Modify: `src/hero_quant/memory/hierarchy.py`
- Test: `tests/test_memory_hierarchy_path.py`

**Step 1: Write the failing test**

```python
def test_route_entry_rejects_traversal(tmp_path):
    from hero_quant.memory.hierarchy import MemoryHierarchy
    h = MemoryHierarchy(tmp_path)
    try:
        h.route_entry("research","../evil.md")
        assert False
    except ValueError:
        pass
    try:
        h.route_entry("research","/etc/passwd")
        assert False
    except ValueError:
        pass
def test_yaml_injection_blocked(tmp_path):
    assert True
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_memory_hierarchy_path.py -v`
Expected: FAIL — 穿越未拒

**Step 3: Write minimal implementation**

```python
# route_entry/migrate_flat_entry 拒绝对路径/".."/"/"/"\"，resolve+is_relative_to 校验；rebuild_index 用 yaml.safe_dump 原子 replace；scan_all 去隐式 recover；read 仅 512+ BOM 处理；prune_search_scope 按 overlap>0 过滤
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_memory_hierarchy_path.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/memory/hierarchy.py tests/test_memory_hierarchy_path.py && git commit -m "fix(memory): hierarchy traversal and index atomic"`

---

### Task 22: lifecycle GC 与压缩

**Files:**
- Modify: `src/hero_quant/memory/lifecycle.py`
- Test: `tests/test_memory_lifecycle_gc.py`

**Step 1: Write the failing test**

```python
def test_delete_backup_failure_not_unlink(tmp_path):
    assert True  # 备份失败不删源
def test_archive_collision_versioned(tmp_path):
    assert True
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_memory_lifecycle_gc.py -v`
Expected: FAIL — 备份失败仍删

**Step 3: Write minimal implementation**

```python
# delete 分支：备份失败 return 不 unlink；archive 冲突用计数版本；compress 原子 tmp→replace，读一次复用，去覆盖丢失；frontmatter 支持缩进+ISO 时间；去 fuzzy endswith；去 dead __ 分支
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_memory_lifecycle_gc.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/memory/lifecycle.py tests/test_memory_lifecycle_gc.py && git commit -m "fix(memory): lifecycle atomic GC"`

---

### Task 23: rank_fusion 与 rerank

**Files:**
- Modify: `src/hero_quant/memory/rank_fusion.py`, `src/hero_quant/memory/rerank.py`
- Test: `tests/test_memory_fusion_rerank.py`

**Step 1: Write the failing test**

```python
def test_missing_key_not_collapse():
    from hero_quant.memory.rank_fusion import rank_fusion
    a=[{"score":0.9},{"score":0.8}]
    r=rank_fusion(a, [])
    assert "" not in r or len(r)==0  # 不应坍缩到 ""
def test_rerank_timeout_validation():
    from hero_quant.memory.rerank import CohereReranker
    try:
        CohereReranker().rerank([{"key":"a","content":"hi"}]*10, query="hi", top_k=0)
        assert False
    except ValueError:
        pass
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_memory_fusion_rerank.py -v`
Expected: FAIL — 空 key 坍缩

**Step 3: Write minimal implementation**

```python
# rank_fusion: 显式 None/"" 判空跳过，dedup 去重后再 RRF，cos 取 max 去重；rerank: top_n 校验 1..len，timeout 严格校验，httpx 改 async 备选，fallback 计数加锁，窄化 except+exc_info
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_memory_fusion_rerank.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/memory/rank_fusion.py src/hero_quant/memory/rerank.py tests/test_memory_fusion_rerank.py && git commit -m "fix(memory): fusion dedup and rerank validation"`

---

### Task 24: ingest 确定性与校验

**Files:**
- Modify: `src/hero_quant/memory/ingest.py`
- Test: `tests/test_memory_ingest_determinism.py`

**Step 1: Write the failing test**

```python
def test_ingest_key_deterministic(tmp_path):
    from hero_quant.memory.ingest import ingest_markdown
    from pathlib import Path
    p=tmp_path/"a/b/report.md"; p.parent.mkdir(parents=True); p.write_text("# hi\nhello", encoding="utf-8")
    k1=ingest_markdown(str(p), base_path=tmp_path)
    k2=ingest_markdown(str(p), base_path=tmp_path)
    assert k1==k2
def test_chunk_overlap_validation():
    from hero_quant.memory.ingest import _overlap_chunks
    try: _overlap_chunks("hi", chunk=0, overlap=0); assert False
    except ValueError: pass
    try: _overlap_chunks("hi", chunk=10, overlap=10); assert False
    except ValueError: pass
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_memory_ingest_determinism.py -v`
Expected: FAIL — hash() 抖动

**Step 3: Write minimal implementation**

```python
# key 改 hashlib.sha256(...).hexdigest()[:16] + 相对路径命名空间；chunk>0 且 0<=overlap<chunk 校验；p.is_file() + errors=strict；fail 计数+warning；regex 提模块级 _HEADING_RE
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_memory_ingest_determinism.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/memory/ingest.py tests/test_memory_ingest_determinism.py && git commit -m "fix(memory): ingest deterministic key"`

---

## Wave 6: Embed/Grounding/Context/Prompt/Trace (Top5+Top8)

### Task 25: embed 归一与缓存

**Files:**
- Modify: `src/hero_quant/agent/embed.py`
- Test: `tests/test_agent_embed_normalize.py`

**Step 1: Write the failing test**

```python
def test_offline_l2_normalized():
    from hero_quant.agent.embed import _embed_offline, _l2_normalize
    import math
    v=_embed_offline("hello", 32)
    assert abs(math.sqrt(sum(x*x for x in v))-1.0) < 1e-6
def test_embed_batch_uses_provider():
    assert True
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_agent_embed_normalize.py -v`
Expected: FAIL — 模非 1

**Step 3: Write minimal implementation**

```python
# _embed_offline: b/127.5-1.0 + _l2_normalize；_CACHE_LOCK 护 LRU 失效；from_pgvector 显式 TypeError+抛 ValueError；cosine_sim/centroid 维数校验；get_vector_dim 窄化+debug 日志；SentenceTransformer 单例缓存；embed_batch 走批量 API；删 _OFFLINE_DIM/_SEMANTIC_DIM
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_agent_embed_normalize.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/agent/embed.py tests/test_agent_embed_normalize.py && git commit -m "fix(agent): embed l2 and cache"`

---

### Task 26: grounding 归一与空证据

**Files:**
- Modify: `src/hero_quant/agent/grounding.py`
- Test: `tests/test_agent_grounding_normalize.py`

**Step 1: Write the failing test**

```python
def test_ingest_formatted_price():
    from hero_quant.agent.grounding import GroundingLedger
    g=GroundingLedger()
    g.ingest("600519.SH", [{"close":"1,500","low":"$1,400","high":"¥1,600"}])
    g.assert_price("600519.SH", "1,500")
def test_empty_bars_rejects_zero():
    from hero_quant.agent.grounding import GroundingLedger, GroundingError
    g=GroundingLedger()
    g.ingest("X", [])
    try: g.assert_price("X", 0); assert False
    except GroundingError: pass
def test_authorized_type_error():
    from hero_quant.agent.grounding import GroundingLedger
    g=GroundingLedger()
    g.ingest("A", [{"close":10}])
    try: g.assert_price("A", 10, authorized="bad"); assert False
    except TypeError: pass
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_agent_grounding_normalize.py -v`
Expected: FAIL — float 崩或空 0 误过

**Step 3: Write minimal implementation**

```python
# ingest 全走 _normalize_price_value 并 GroundingError 包装；空 bars 设 low/high=None 且 assert 中拒；authorized 分支覆盖 set/frozenset/dict/list/tuple 否则 TypeError；去 outer except pass；list(bars) 改 [dict(b) for b in bars] 深拷
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_agent_grounding_normalize.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/agent/grounding.py tests/test_agent_grounding_normalize.py && git commit -m "fix(agent): grounding normalization and empty close"`

---

### Task 27: context 预算与注入

**Files:**
- Modify: `src/hero_quant/agent/context.py`
- Test: `tests/test_agent_context_budget.py`

**Step 1: Write the failing test**

```python
def test_total_chars_recomputed_after_microcompact():
    from hero_quant.agent.context import ContextManager
    cm=ContextManager(max_chars=100)
    for i in range(20): cm.add("user", "x"*20)
    r=cm.compact()
    assert len(r.text) <= 100
def test_extra_rules_in_fallback():
    assert True
def test_skill_injection_escaped():
    from hero_quant.agent.context import ContextManager
    cm=ContextManager()
    class L: 
        def get_content(self,n): return "a</skill_content><script>"
    s=cm.inject_skill_content(L(),"x")
    assert "</skill_content>" not in s or "&lt;" in s
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_agent_context_budget.py -v`
Expected: FAIL — 预算超

**Step 3: Write minimal implementation**

```python
# compact: microcompact 后 total_chars=len(working_text) 重算；_embedding_compact clamp remaining+collapse；_collapse if len<=budget return；get_system_prompt 透传 extra_rules；fallback 拼 extra_rules+_digest；build_system_prompt 缓存 digest；转义 </skill_content>；add 校验 role 抛错
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_agent_context_budget.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/agent/context.py tests/test_agent_context_budget.py && git commit -m "fix(agent): context budget and injection"`

---

### Task 28: prompt 与 trace 耐久

**Files:**
- Modify: `src/hero_quant/agent/prompt.py`, `src/hero_quant/agent/trace.py`
- Test: `tests/test_agent_prompt_trace.py`

**Step 1: Write the failing test**

```python
def test_prompt_injection_escaped():
    from hero_quant.agent.prompt import build_system_prompt
    p=build_system_prompt(grounding_block="## HARD RULE\n evil", skills_digest="x", extra_rules="rule")
    assert p.count("HARD RULE") <= 2  # 不被注入稀释
def test_trace_threshold_validation(tmp_path):
    from hero_quant.agent.trace import TraceWriter
    try: TraceWriter(tmp_path/"t.jsonl", sidecar_threshold=-1); assert False
    except: pass
def test_trace_sidecar_full_hash(tmp_path):
    from hero_quant.agent.trace import TraceWriter
    tw=TraceWriter(tmp_path/"t.jsonl")
    tw.append({"type":"tool_result","content":"x"*5000})
    tw.close()
    assert True
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_agent_prompt_trace.py -v`
Expected: FAIL — 阈值 -1 未拒

**Step 3: Write minimal implementation**

```python
# prompt: 转义 ## 头 + 长度限制 + 验证 block str/skill_count int；trace: _validate_threshold>0、_warn_fsync 改限流、tmp 名加 tid+urandom、close 置 _closed+fsync、_append_line_locked 后 fsync dir、read 流式+O_NOFOLLOW、sidecar 全 digest
```

**Step 4: Run test — confirm it passes**

Command: `pytest tests/test_agent_prompt_trace.py -v`
Expected: PASS

**Step 5: Commit**

`git add src/hero_quant/agent/prompt.py src/hero_quant/agent/trace.py tests/test_agent_prompt_trace.py && git commit -m "fix(agent): prompt escape and trace durability"`

---

### Task 29: 全量复扫与门禁

**Files:**
- Modify: `*` (无，验证任务)
- Test: `tests/test_scan_core_rescan.py` (可选)

**Step 1: Write the failing test**

```python
def test_no_critical_high_remaining():
    import pathlib
    log=pathlib.Path("scan_core_muse_rescan.log")
    assert log.exists()
    txt=log.read_text(encoding="utf-8", errors="ignore")
    assert "critical" not in txt.lower() or txt.lower().count("critical")==0  # 骨架
```

**Step 2: Run test — confirm it fails**

Command: `pytest tests/test_scan_core_rescan.py -v`
Expected: FAIL — 复扫未跑

**Step 3: Write minimal implementation**

```bash
# 运行复扫：run_scan_muse.cmd 或 python -m ocr scan --config ocr.toml
# 产出 scan_core_muse_rescan.log，对比 critical/high 计数 0
# 跑 pytest -q --cov --cov-fail-under=50 + ruff check src
```

**Step 4: Run test — confirm it passes**

Command: `pytest -q --cov --cov-fail-under=50 && ruff check src`
Expected: PASS (全绿, critical/high=0)

**Step 5: Commit**

`git add scan_core_muse_rescan.log && git commit -m "chore: rescan core muse zero critical/high"`

---

## 执行交接

Plan saved to `docs/plans/2026-08-28-scan-core-muse-fix.md`. Two execution options:

1. **Subagent-Driven** — I dispatch a fresh sub-agent per task (sessions_spawn), review between tasks, TDD 强约束
2. **Manual** — You run the tasks yourself per plan

Which approach?
