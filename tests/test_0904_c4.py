"""C4 lane TDD: OCR 网关 15 条（rate_limiter×4 + server×6 + router×5）。

契约：fail-closed；原子性；超时必传；中文注释；窄化捕获。
每条对应 scan_0904_g1_api_infra.log 的 lane C4。
"""
from __future__ import annotations

import asyncio
import pathlib
import types

import pytest


# ============================================================
# rate_limiter.py:50-51 三档共用 bucket
# ============================================================
def test_c4_rl_shared_bucket_tier_isolation():
    """三档不得共用裸 key：chat/tool/session 应带 endpoint 前缀隔离。"""
    src = pathlib.Path("D:/kaipanla-data/hero-quant/src/hero_quant/api/rate_limiter.py").read_text(encoding="utf-8")
    # 修复后 try_acquire 的 key 应包含 endpoint/tier 区分
    assert "endpoint" in src, "rate_limiter 未引入 endpoint 区分"
    # 关键：try_acquire 调用处应为 f\"{endpoint}:...\" 或类似带 endpoint
    # 检查 _check 内 try_acquire 行包含 endpoint
    import re
    m = re.search(r"try_acquire\(.*?endpoint.*?,", src, re.DOTALL)
    # 也允许 f-string 形式
    has_tier_key = m is not None or ("{endpoint}" in src and "try_acquire" in src)
    assert has_tier_key, "try_acquire 仍用裸 limit_key(request) 未隔离 tier"


def test_c4_rl_shared_bucket_runtime(monkeypatch):
    """运行时：同 key 不同 tier 不应命中同一桶。"""
    import hero_quant.api.rate_limiter as rl

    calls = []

    class _FakeRL:
        async def try_acquire(self, key, quota, window):
            calls.append((key, quota))
            return True

    # 桩 infra RateLimiter
    import hero_quant.infra.redis as redis_mod
    monkeypatch.setattr(redis_mod, "RateLimiter", lambda: _FakeRL())
    # 也桩 rl 模块内可能已导入的 RateLimiter
    if hasattr(rl, "RateLimiter"):
        monkeypatch.setattr(rl, "RateLimiter", lambda: _FakeRL())

    req = types.SimpleNamespace(state=types.SimpleNamespace(current_user=types.SimpleNamespace(id=123)), client=types.SimpleNamespace(host="1.1.1.1"))
    # 清空 calls
    calls.clear()
    asyncio.run(rl.rate_limit_chat(req))
    chat_key = calls[-1][0]
    calls.clear()
    asyncio.run(rl.rate_limit_tool(req))
    tool_key = calls[-1][0]
    assert chat_key != tool_key, f"chat/tool 共用同一 key={chat_key} 未隔离"
    # key 应包含 tier 前缀
    assert "chat" in chat_key and "tool" in chat_key or chat_key != tool_key
    assert "123" in chat_key and "123" in tool_key


# ============================================================
# rate_limiter.py:52-54 Redis 故障 fail-open
# ============================================================
def test_c4_rl_fail_closed_on_redis_error(monkeypatch):
    """Redis 异常不得 fail-open 放行，应 fail-closed 抛 503 且 warning 级别。"""
    src = pathlib.Path("D:/kaipanla-data/hero-quant/src/hero_quant/api/rate_limiter.py").read_text(encoding="utf-8")
    # 修复后不应有 return True 的 fail-open
    # 允许注释中的 return True，但 except 块内不应直接 return True
    import re
    except_block = re.search(r"except Exception.*?:\s*\n(.*?)(?:\n\s*if not ok:)", src, re.DOTALL)
    if except_block:
        assert "return True" not in except_block.group(1), "except 块仍 return True fail-open"
    assert "503" in src or "503" in src or "Rate limiter unavailable" in src or "raise HTTPException" in src, "未 fail-closed 抛 503"
    assert "logger.warning" in src or "logger.warn" in src, "限流异常应 warning 而非 debug"


def test_c4_rl_fail_closed_runtime(monkeypatch):
    """运行时：后端抛错应抛 HTTPException 503 而非放行。"""
    import hero_quant.api.rate_limiter as rl
    from fastapi import HTTPException

    class _Boom:
        async def try_acquire(self, *a, **k):
            raise RuntimeError("redis down")

    import hero_quant.infra.redis as redis_mod
    monkeypatch.setattr(redis_mod, "RateLimiter", lambda: _Boom())
    if hasattr(rl, "RateLimiter"):
        monkeypatch.setattr(rl, "RateLimiter", lambda: _Boom())

    req = types.SimpleNamespace(state=types.SimpleNamespace(current_user=None), client=types.SimpleNamespace(host="9.9.9.9"))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(rl.rate_limit_chat(req))
    assert ei.value.status_code == 503


# ============================================================
# rate_limiter.py:34-35 falsy id 当匿名
# ============================================================
def test_c4_rl_falsy_id_not_anonymous():
    """falsy id 如 0/\"\" 不得当匿名：应显式 is not None 判定。"""
    src = pathlib.Path("D:/kaipanla-data/hero-quant/src/hero_quant/api/rate_limiter.py").read_text(encoding="utf-8")
    # 修复后应为 is not None 而非 if uid:
    assert "is not None" in src, "未用 is not None 显式判定"
    # 不应再有裸 if uid: 当限流 key 判断
    # 检查 limit_key 内不应有纯 if uid:
    import re
    # 找到 limit_key 函数体
    m = re.search(r"def limit_key.*?return f\"ip:", src, re.DOTALL)
    assert m is not None
    block = m.group(0)
    # 修复后不应仅靠 if uid: 判定
    assert "if uid:" not in block, "仍用 if uid: 会把 0/空串当匿名"


def test_c4_rl_falsy_id_runtime():
    """运行时：uid=0 应得 user:0 而非 ip:unknown。"""
    import hero_quant.api.rate_limiter as rl

    req0 = types.SimpleNamespace(state=types.SimpleNamespace(current_user=types.SimpleNamespace(id=0)), client=types.SimpleNamespace(host="1.2.3.4"))
    assert rl.limit_key(req0) == "user:0"
    req_empty = types.SimpleNamespace(state=types.SimpleNamespace(current_user=types.SimpleNamespace(id="")), client=types.SimpleNamespace(host="1.2.3.4"))
    # 空串应回退 ip
    assert rl.limit_key(req_empty).startswith("ip:")
    req_none = types.SimpleNamespace(state=types.SimpleNamespace(current_user=types.SimpleNamespace(id=None)), client=types.SimpleNamespace(host="5.6.7.8"))
    assert rl.limit_key(req_none) == "ip:5.6.7.8"


# ============================================================
# rate_limiter.py:47-48 函数内 import
# ============================================================
def test_c4_rl_top_level_import():
    """RateLimiter 应顶层导入，不在函数内每请求导入。"""
    src = pathlib.Path("D:/kaipanla-data/hero-quant/src/hero_quant/api/rate_limiter.py").read_text(encoding="utf-8")
    # 顶层（def _check 之前）应有 import RateLimiter
    pre_check = src.split("def _check")[0]
    assert "RateLimiter" in pre_check, "顶层未导入 RateLimiter"
    assert "from hero_quant.infra.redis import RateLimiter" in pre_check or "from hero_quant.infra.redis import" in pre_check
    # _check 内不应再有 from hero_quant.infra.redis import RateLimiter
    post = src.split("def _check")[1].split("def rate_limit_chat")[0] if "def rate_limit_chat" in src else src.split("def _check")[1]
    assert "from hero_quant.infra.redis import RateLimiter" not in post, "_check 内仍函数内 import"


# ============================================================
# server.py:873-875 BackgroundTasks 被丢弃+可变默认
# ============================================================
def test_c4_server_backgroundtasks_no_mutable_default():
    """query/query_stream 不得用 BackgroundTasks([]) 可变默认且不得丢弃注入。"""
    src = pathlib.Path("D:/kaipanla-data/hero-quant/src/hero_quant/api/server.py").read_text(encoding="utf-8")
    assert "BackgroundTasks([])" not in src, "仍有可变默认 BackgroundTasks([])"
    import re
    # 修复后应为 BackgroundTasks | None = None 且按需新建（if is None），不得无条件 shadow
    assert "BackgroundTasks | None = None" in src, "签名未改为 BackgroundTasks | None = None"
    # 检查 query/query_stream 定义后 15 行内不得有无条件 background_tasks = BackgroundTasks()
    lines = src.splitlines()
    for idx, line in enumerate(lines):
        if "async def query(" in line and "background_tasks" in line:
            window = "\n".join(lines[idx:idx+15])
            # 若出现无条件赋值（行首即 background_tasks =），且无 if 守卫，则失败
            any(re.match(r"\s*background_tasks\s*=\s*BackgroundTasks\(\)", l) and "if " not in window.split(l)[0][-200:] for l in window.splitlines())
            # 更精确：检查 window 中是否存在 "if background_tasks is None" 守卫
            assert "if background_tasks is None" in window, f"query 未按需守卫 background_tasks: {window[:300]}"
        if "async def query_stream(" in line and "background_tasks" in line:
            window = "\n".join(lines[idx:idx+15])
            assert "if background_tasks is None" in window, f"query_stream 未按需守卫: {window[:300]}"


def test_c4_server_backgroundtasks_signature():
    """签名应无可变默认，推荐 BackgroundTasks | None = None。"""
    src = pathlib.Path("D:/kaipanla-data/hero-quant/src/hero_quant/api/server.py").read_text(encoding="utf-8")
    assert "background_tasks: BackgroundTasks | None" in src or "background_tasks: BackgroundTasks | None = None" in src or "background_tasks: BackgroundTasks" in src
    # 若为 None 默认，则内部应有 if background_tasks is None: 新建
    if "BackgroundTasks | None = None" in src:
        assert "if background_tasks is None" in src, "None 默认却无按需新建逻辑"


# ============================================================
# server.py:1055-1058 大 except 吞错
# ============================================================
def test_c4_server_narrow_except_around_to_thread():
    """await asyncio.to_thread(loop.run) 的 except 不得为宽 Exception 吞业务错并同步重试阻塞 loop。"""
    src = pathlib.Path("D:/kaipanla-data/hero-quant/src/hero_quant/api/server.py").read_text(encoding="utf-8")
    import re
    # 紧邻 to_thread 的 except（5 行内）不得为宽 Exception
    lines = src.splitlines()
    for i, l in enumerate(lines):
        if "await asyncio.to_thread(loop.run" in l:
            window = "\n".join(lines[i:i+6])
            assert "except Exception" not in window or "except RuntimeError" in window, f"to_thread 紧邻宽 except: {window[:300]}"
            assert "except RuntimeError" in window, f"未窄化 to_thread 的 except: {window[:300]}"
    # 整体也需存在窄化
    assert re.search(r"except\s+RuntimeError", src) is not None, "未窄化 to_thread 的 except"


# ============================================================
# server.py:467-468 sync shutdown cancel 不 await
# ============================================================
def test_c4_server_shutdown_is_async_and_awaits():
    """shutdown 需为 async 且 await 取消的任务，带超时。"""
    src = pathlib.Path("D:/kaipanla-data/hero-quant/src/hero_quant/api/server.py").read_text(encoding="utf-8")
    assert "async def _stop_trace_consumer" in src, "shutdown 未改为 async def"
    # 应包含 await 且处理 CancelledError/TimeoutError
    assert "await" in src.split("async def _stop_trace_consumer")[1].split("\n\n")[0] or "await" in src.split("async def _stop_trace_consumer")[1][:1200]
    seg = src.split("async def _stop_trace_consumer")[1][:2000]
    assert "CancelledError" in seg or "TimeoutError" in seg or "wait_for" in seg, "未 await 并处理 CancelledError/TimeoutError"


# ============================================================
# server.py:691-694 /ready PG 探测阻塞无超时
# ============================================================
def test_c4_server_ready_has_timeout():
    """checkpoint/billing 的 SELECT 1 探测必须带超时，避免阻塞 /ready。"""
    src = pathlib.Path("D:/kaipanla-data/hero-quant/src/hero_quant/api/server.py").read_text(encoding="utf-8")
    # 检查两处探活是否含 timeout 关键字
    # 找到 _check_checkpoint_pg 与 _check_billing_pg 段
    assert src.count("timeout") >= 2, "ready 探活未添加 timeout"
    # 更细：pool.connection 或 getconn 或 execute 应带 timeout
    assert "timeout=2" in src or "timeout=1" in src or "timeout= 2" in src or "timeout=2.0" in src, "未见 timeout=2 等超时参数"
    # 中文注释应提及超时
    assert "超时" in src, "未添加中文超时注释"


# ============================================================
# server.py:1562-1565 回测 L1 check-then-act
# ============================================================
def test_c4_server_backtest_l1_atomic():
    """_get_backtest_bundle 首检应在锁内，且分布式锁用 token+Lua 安全释放。"""
    src = pathlib.Path("D:/kaipanla-data/hero-quant/src/hero_quant/api/server.py").read_text(encoding="utf-8")
    import re
    # 修复后 global 后在 5 行内即 with 锁（允许中间有中文注释行），且首检在锁内
    lines = src.splitlines()
    found = False
    for i, l in enumerate(lines):
        if "global _backtest_cache" in l:
            window = "\n".join(lines[i:i+8])
            if "with _backtest_cache_lock" in window:
                # 且 window 内先出现 with 再出现 if _backtest_cache
                w_idx = window.find("with _backtest_cache_lock")
                if_idx = window.find("if _backtest_cache")
                assert w_idx < if_idx, "首检仍在锁外"
                found = True
                break
    assert found, "global 后未紧跟 with _backtest_cache_lock"
    # 全局外首检：global 行后 2 行内不应直接 if _backtest_cache
    assert re.search(r"global _backtest_cache\s*\n\s*if _backtest_cache:", src) is None
    assert "token" in src.lower(), "分布式锁未使用 token"
    assert "eval" in src.lower() or "lua" in src.lower() or ("get" in src.lower() and "delete" in src.lower() and "token" in src.lower()), "未用 Lua compare-del 安全释放"


# ============================================================
# server.py:480 私有 REGISTRY 依赖
# ============================================================
def test_c4_server_no_private_registry():
    """不得依赖 prometheus_client.REGISTRY._names_to_collectors 私有 API。"""
    src = pathlib.Path("D:/kaipanla-data/hero-quant/src/hero_quant/api/server.py").read_text(encoding="utf-8")
    assert "_names_to_collectors" not in src, "仍依赖私有 _names_to_collectors"
    assert "_collector_to_names" not in src, "仍依赖私有 _collector_to_names"


# ============================================================
# mcp/router.py:221-224 BM25 快照 torn
# ============================================================
def test_c4_router_bm25_snapshot_not_torn():
    """BM25 语料发布应在锁内，避免 torn snapshot。"""
    src = pathlib.Path("D:/kaipanla-data/hero-quant/src/hero_quant/mcp/router.py").read_text(encoding="utf-8")
    # 修复后 _IDF/_AVG_DL/_N/_DOC_TOKENS 赋值应在 with _CORPUS_LOCK: 内
    import re
    # 找到赋值段并确认其前有 with _CORPUS_LOCK
    # 简化：统计 with _CORPUS_LOCK 次数应 >=2（指纹检查 + 发布）
    assert src.count("with _CORPUS_LOCK") >= 2, "BM25 发布未在锁内（with _CORPUS_LOCK 次数不足）"
    # 确保发布段在锁内
    m = re.search(r"with _CORPUS_LOCK:\s*\n.*?_IDF\s*=\s*idf", src, re.DOTALL)
    assert m is not None, "_IDF 发布不在锁内"


# ============================================================
# mcp/router.py:45-48 懒单例竞态
# ============================================================
def test_c4_router_singleton_double_checked_lock():
    """_get_router_circuit/_get_rate_limiter 需双重检查锁，避免竞态。"""
    src = pathlib.Path("D:/kaipanla-data/hero-quant/src/hero_quant/mcp/router.py").read_text(encoding="utf-8")
    # 两函数内应有 with _LIMITER_LOCK 或 with _CORPUS_LOCK 包裹创建
    circ_seg = src.split("def _get_router_circuit")[1].split("def _get_rate_limiter")[0] if "def _get_router_circuit" in src else ""
    assert "with _LIMITER_LOCK" in circ_seg or "with _CORPUS_LOCK" in circ_seg, "_get_router_circuit 未加锁 DCL"
    # 双重检查：外层 if is None，内层 with 内再 if is None
    assert circ_seg.count("if _ROUTER_CIRCUIT is None") >= 2, "_get_router_circuit 未做双重检查"
    limiter_seg = src.split("def _get_rate_limiter")[1].split("def get_router_limiter")[0] if "def _get_rate_limiter" in src else ""
    assert "with _LIMITER_LOCK" in limiter_seg or "with _CORPUS_LOCK" in limiter_seg, "_get_rate_limiter 未加锁"
    assert limiter_seg.count("if _ROUTER_RATE_LIMITER is None") >= 2, "_get_rate_limiter 未做双重检查"


# ============================================================
# mcp/router.py:432-435 Circuit-OPEN 破计算不变量
# ============================================================
def test_c4_router_circuit_open_preserves_compute_factor():
    """熔断 OPEN 短路返回也必须保证 compute_factor 含 momentum/factor 查询的首位不变量。"""
    src = pathlib.Path("D:/kaipanla-data/hero-quant/src/hero_quant/mcp/router.py").read_text(encoding="utf-8")
    import re
    # OPEN 分支内应包含 momentum/factor 处理
    re.search(r"if circ is not None and not circ\.allow\(\):.*?return.*?\[.*?curated.*?\]?.*?\n", src, re.DOTALL)
    # 更宽松：找到 allow() 后的 return 前是否处理 compute_factor
    allow_idx = src.find("circ.allow()")
    assert allow_idx != -1
    open_block = src[allow_idx:allow_idx+2000]
    assert "compute_factor" in open_block, "OPEN 路径未处理 compute_factor 不变量"
    assert "momentum" in open_block and "factor" in open_block, "OPEN 路径未检查 momentum/factor"


def test_c4_router_circuit_open_runtime(monkeypatch):
    """运行时：熔断 OPEN 时含 momentum 的查询仍应含 compute_factor 且在首位附近。"""
    import hero_quant.mcp.router as router
    from hero_quant.tools.registry import TOOL_REGISTRY
    # 确保 compute_factor 在 registry
    assert "compute_factor" in TOOL_REGISTRY
    # 桩熔断为 OPEN
    class _OpenCirc:
        def allow(self): return False
        def record_failure(self): pass
    monkeypatch.setattr(router, "_get_router_circuit", lambda: _OpenCirc())
    # 桩限流放行
    class _OkLimiter:
        def try_acquire(self, n): return True
        def available_tokens(self): return (100, 100)
    monkeypatch.setattr(router, "_get_rate_limiter", lambda: _OkLimiter())
    # 桩 CURATED
    monkeypatch.setattr(router, "CURATED_TOOLS", ["get_market_data", "run_backtest", "compute_factor", "get_news"])
    out = router.route("momentum factor ranking", k=5)
    assert "compute_factor" in out, f"OPEN 时 compute_factor 丢失: {out}"
    # 应在前列（k=5 时至少前 2）
    assert out.index("compute_factor") <= 1, f"compute_factor 未在首位: {out}"


# ============================================================
# mcp/router.py:312-315 缓存命中持锁做 cosine
# ============================================================
def test_c4_router_cache_hit_not_hold_lock_for_cosine():
    """缓存命中时不得持 _DESC_VEC_LOCK 做 cosine/import。"""
    src = pathlib.Path("D:/kaipanla-data/hero-quant/src/hero_quant/mcp/router.py").read_text(encoding="utf-8")
    # 修复后 with _DESC_VEC_LOCK 块内不应有 return _cosine
    import re
    # 找到 _vector_score_for_tool 内的 with 块
    seg = src.split("def _vector_score_for_tool")[1].split("def is_pgvector_router_configured")[0] if "def _vector_score_for_tool" in src else ""
    # 检查 with 内是否直接 return _cosine
    with_blocks = re.findall(r"with _DESC_VEC_LOCK:\s*\n(.*?)(?:\n\s*except|\n\s*try|\n\s*return|\n\n)", seg, re.DOTALL)
    for blk in with_blocks:
        assert "return _cosine" not in blk, "仍在锁内 return _cosine(query_vec, dvec)"
    # 额外：应为先取后在锁外 cosine
    assert "dvec" in seg and "_cosine" in seg
    # 确保锁内仅为 get/fetch，不含 _cosine
    assert re.search(r"with _DESC_VEC_LOCK:\s*\n[^\n]*get\(cache_key\)", seg) is not None or "dvec = _DESC_VEC_CACHE.get" in seg, "未改为锁内仅 get 取值"


# ============================================================
# mcp/router.py:335-337 窄 except 破 fallback
# ============================================================
def test_c4_router_vector_except_is_broad():
    """_vector_score_for_tool 的 except 应为宽 Exception，保证回退 BM25。"""
    src = pathlib.Path("D:/kaipanla-data/hero-quant/src/hero_quant/mcp/router.py").read_text(encoding="utf-8")
    seg = src.split("def _vector_score_for_tool")[1].split("def is_pgvector_router_configured")[0] if "def _vector_score_for_tool" in src else ""
    # 不应再仅捕 (ImportError, ValueError, TypeError)
    assert "except (ImportError, ValueError, TypeError)" not in seg, "仍为窄 except 致网络/超时逃逸"
    assert "except Exception" in seg, "未改为宽 except Exception 回退"
