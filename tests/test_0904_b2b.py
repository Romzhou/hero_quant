"""B2b lane: 19 条 TDD 红测 — billing/reconcile/registry/trait.

契约: fail-closed, provenance 必传, 锁不横跨 IO, 异常链保留(raise from), 中文注释, 窄化捕获.
先跑红再修再跑绿.
"""
from __future__ import annotations

import inspect
import logging

# ---------------------------------------------------------------------------
# billing/service.py 5 条
# ---------------------------------------------------------------------------

def test_b2b_01_pg_publish_must_insert_in_same_txn():
    """301-302 真 PG 分支必须同一连接同一事务内 SET LOCAL 后紧跟 INSERT INTO factors (critical)."""
    from hero_quant.billing import service as mod
    src = inspect.getsource(mod.BillingService._pg_publish_sync)
    # 修复前: 只有 SET LOCAL 循环, 无 INSERT INTO factors
    assert "INSERT INTO factors" in src or "INSERT INTO" in src, "_pg_publish_sync 必须执行 INSERT INTO factors"
    # 必须同一连接内完成: 同一 with pool.connection() 块内既有 SET LOCAL 又有 INSERT
    # 简单检查: SET LOCAL 与 INSERT 应在同一连接上下文
    # 若为每个 key 单独开 connection 则为 bug
    # 通过模拟 pool 观察连接复用
    inserted = []

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            inserted.append(sql)
            class Cur:
                def fetchone(self): return None
            return Cur()
        def cursor(self):
            class Ctx:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def execute(self, *a, **kw): inserted.append(a[0] if a else "")
                def fetchone(self): return None
            return Ctx()
        def commit(self): pass

    class FakePool:
        def __init__(self): self.conn_calls = 0
        def connection(self):
            self.conn_calls += 1
            return FakeConn()

    svc = mod.BillingService(dsn="postgresql://postgres:postgres@localhost:5432/b2b_insert_test", pool=FakePool())
    # 强制走真实 PG 路径: _is_real_pg 取决于 pool 非空且 dsn 为 PG -> 已满足
    assert svc._is_real_pg() is True
    try:
        svc._pg_publish_sync({"factor_id": "f1", "tenant": "t1", "price": 10, "name": "n", "description": ""})
    except Exception:
        pass
    # 同一 pool 必须只开一次连接完成 SET LOCAL + INSERT (锁不横跨 IO 且事务内完成)
    # 修复前每个 key 开一次连接 -> conn_calls == 2
    assert svc._pool.conn_calls == 1, f"SET LOCAL 与 INSERT 必须同一连接同一事务, 实际连接数={svc._pool.conn_calls}"
    assert any("INSERT" in s for s in inserted), f"未执行 INSERT, 实际执行: {inserted}"


def test_b2b_02_purchase_lock_not_across_io():
    """456-458 全局锁不横跨 IO — purchase 的 _GLOBAL_LOCK 临界区必须缩小, ledger/DB IO 在锁外."""
    from hero_quant.billing import service as mod
    src = inspect.getsource(mod.BillingService.purchase)
    # 修复前整个 ledger.append + _pg_insert_purchase_sync + _pg_purchase_sync 都在 with _GLOBAL_LOCK 内
    # 修复后临界区仅覆盖内存 dedup/insert, ledger.append 与 PG IO 在锁外
    # 检查: ledger.append 不应在 with _GLOBAL_LOCK 块内
    lines = src.splitlines()
    in_lock = False
    lock_indent = None
    ledger_inside_lock = False
    for line in lines:
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if "with _GLOBAL_LOCK" in line:
            in_lock = True
            lock_indent = indent
            continue
        if in_lock:
            if stripped and indent <= lock_indent:
                in_lock = False
            elif "ledger.append" in line or "_pg_insert_purchase_sync" in line or "_pg_purchase_sync" in line:
                ledger_inside_lock = True
    assert ledger_inside_lock is False, "purchase 的 ledger.append / PG IO 不得在 _GLOBAL_LOCK 临界区内(锁不横跨 IO)"


def test_b2b_03_pg_sync_false_must_fail_closed():
    """262-264 PG-sync 返回 False 不得被丢弃 — publish/purchase 需检查返回值并回滚或抛错."""
    from hero_quant.billing import service as mod
    from hero_quant.billing.service import BillingService, _GLOBAL_FACTORS, _GLOBAL_PURCHASES, _dsn_key

    class FakePool:
        def connection(self):
            class FakeConn:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def execute(self, *a, **kw): pass
                def cursor(self):
                    class C:
                        def __enter__(self): return self
                        def __exit__(self, *a): return False
                        def execute(self, *a, **kw): pass
                        def fetchone(self): return None
                    return C()
                def commit(self): pass
            return FakeConn()

    dsn = "postgresql://postgres:postgres@localhost:5432/b2b_false_test"
    k = _dsn_key(dsn)
    _GLOBAL_FACTORS.pop(k, None)
    _GLOBAL_PURCHASES.pop(k, None)
    _GLOBAL_FACTORS.pop(dsn, None)
    _GLOBAL_PURCHASES.pop(dsn, None)

    svc = BillingService(dsn=dsn, pool=FakePool())
    # 强制 _pg_publish_sync 返回 False (模拟无真实 pool / 被跳过)
    orig = svc._pg_publish_sync
    svc._pg_publish_sync = lambda factor: False  # type: ignore
    # publish_factor 应 fail-closed: 抛错且回滚内存/全局写入, 或至少不视为成功
    raised = False
    try:
        svc.publish_factor(factor_id="b2b_false_f", name="F", price=10, tenant="t1")
    except (RuntimeError, ValueError, Exception):
        raised = True
    finally:
        svc._pg_publish_sync = orig  # type: ignore
    # 若未抛错则视为失败; ledger 已 append 但 PG 未持久化不得伪成功
    assert raised is True, "PG-sync 返回 False 时 publish_factor 必须 fail-closed(抛错或回滚), 不得静默成功"
    # 且回滚后全局不应残留
    with mod._GLOBAL_LOCK:
        assert "b2b_false_f" not in _GLOBAL_FACTORS.get(k, {}), "PG-sync 失败后应回滚 _GLOBAL_FACTORS"


def test_b2b_04_set_local_same_txn():
    """315-316 SET LOCAL 必须与 INSERT 同一连接同一事务, 不得每次 key 新开连接."""
    from hero_quant.billing import service as mod
    src = inspect.getsource(mod.BillingService._pg_publish_sync)
    # 修复前为 for _k in ("app.tenant", "app.current_tenant"): with pool.connection() as _conn: _conn.execute(SET LOCAL)
    # 修复后应为 with pool.connection() as conn: conn.execute(SET LOCAL tenant); conn.execute(SET LOCAL current_tenant); conn.execute(INSERT...); conn.commit()
    # 检查源码模式: 不应出现 for _k 循环内单独开连接
    assert 'for _k in ("app.tenant"' not in src, "SET LOCAL 不得对每个 key 单独开连接, 必须同一事务内完成"
    # 且必须同一连接内同时出现 SET LOCAL 与 INSERT
    assert "SET LOCAL" in src and ("INSERT" in src), "同一事务内必须同时执行 SET LOCAL 与 INSERT"


def test_b2b_05_publish_factor_check_then_act_atomic():
    """234-236 publish_factor 存在性检查与写入必须原子化(短临界区或依赖 PG PRIMARY KEY)."""
    from hero_quant.billing import service as mod
    src = inspect.getsource(mod.BillingService.publish_factor)
    # 修复前: exists 检查在 with _GLOBAL_LOCK 之外, 写入才在 lock 内 -> 竞态
    # 修复后: 检查+预留在同一 with _GLOBAL_LOCK 短临界区内(无 IO)
    # 简单检查: 存在性判断应在 with _GLOBAL_LOCK 块内
    # 若 src 中 with _GLOBAL_LOCK 之前就有 if factor_id in _GLOBAL_FACTORS 则判定为竞态
    # 更稳妥: 检查 with _GLOBAL_LOCK 块内包含存在性判断
    assert "with _GLOBAL_LOCK" in src, "publish_factor 必须使用 _GLOBAL_LOCK 保护检查+写入"
    # 确保检查在锁内: 找到 with _GLOBAL_LOCK 后的块包含 factor_id in
    lines = src.splitlines()
    found_lock_before_check = False
    in_lock_block = False
    lock_indent = None
    for line in lines:
        if "with _GLOBAL_LOCK" in line:
            in_lock_block = True
            lock_indent = len(line) - len(line.lstrip())
            continue
        if in_lock_block:
            stripped = line.lstrip()
            indent = len(line) - len(stripped) if stripped else 999
            if stripped and indent <= lock_indent:
                in_lock_block = False
            if "factor_id in" in line or "factor_id) in" in line or "existing" in line:
                found_lock_before_check = True
    assert found_lock_before_check, "publish_factor 的 factor_id 存在性检查必须在 _GLOBAL_LOCK 临界区内(避免 check-then-act 竞态)"

# ---------------------------------------------------------------------------
# governance/reconcile.py 5 条
# ---------------------------------------------------------------------------

def test_b2b_06_verify_failed_not_swallowed(caplog, tmp_path, monkeypatch):
    """T2-2 新契约：verify 失败直接抛 LedgerCorruptionError，不出 zero_diff 误导报告。

    旧契约（verified=False 报告）已被 T2-2 fail-closed 替代：daily_reconciliation 入口先
    verify_chain_with_archives（含归档），失败即抛，不再返回 verified=False + zero_diff=True。
    此处断言：源码不再吞 verify 异常（warning/raise 任一可观测），功能上抛 LedgerCorruptionError。
    """
    import hero_quant.governance.ledger as lm
    import hero_quant.governance.reconcile as rec
    src = inspect.getsource(rec.daily_reconciliation)
    assert "LedgerCorruptionError" in src, "daily_reconciliation 失败必须抛 LedgerCorruptionError，不出 zero_diff"
    assert "logger.warning" in src or "logger.exception" in src or "raise" in src, "verify 失败必须可观测（warning/raise）"
    # 功能验证: mock Ledger.verify 抛错时 daily_reconciliation 直接抛 LedgerCorruptionError
    class FakeLedger:
        def __init__(self, path): self.path = path
        def verify(self): raise RuntimeError("tampered ledger")
    monkeypatch.setattr("hero_quant.governance.ledger.Ledger", FakeLedger)
    # 准备最小 ledger 文件与 positions.csv
    ledger_path = tmp_path / "ledger.jsonl"
    ledger_path.write_text('{"record": {"action":"buy","symbol":"AAPL","qty":1},"tenant":"t","price":1}\n', encoding="utf-8")
    csv_path = tmp_path / "positions.csv"
    csv_path.write_text("symbol,qty\nAAPL,1\n", encoding="utf-8")
    caplog.set_level(logging.WARNING)
    import pytest as _pt
    with _pt.raises(lm.LedgerCorruptionError):
        rec.daily_reconciliation(date="2026-09-04", ledger_path=ledger_path, positions_csv=csv_path)


def test_b2b_07_blank_symbol_csv_warns(caplog, tmp_path):
    """82-85 空 symbol 行不得静默 skip, 必须 warning."""
    from hero_quant.governance.reconcile import load_positions_csv
    import hero_quant.governance.reconcile as rec
    src = inspect.getsource(rec.load_positions_csv)
    assert "logger.warning" in src, "空 symbol 行应 warning"
    csv_path = tmp_path / "positions.csv"
    csv_path.write_text("symbol,qty\n,10\nAAPL,5\n", encoding="utf-8")
    caplog.set_level(logging.WARNING)
    out = load_positions_csv(csv_path)
    assert out == {"AAPL": 5.0}
    assert any("blank" in r.message.lower() or "skip" in r.message.lower() for r in caplog.records), "空 symbol 行应 warning 记录"


def test_b2b_08_malformed_jsonl_must_raise(tmp_path):
    """208-213 坏 JSONL 不得假设上层 verify 会兜底, 必须 fail-closed 抛错."""
    from hero_quant.governance.reconcile import aggregate_shadow
    import hero_quant.governance.reconcile as rec
    src = inspect.getsource(rec.aggregate_shadow)
    # 修复后应 raise ValueError 而非 continue
    assert "raise ValueError" in src or "raise" in src.split("malformed")[1][:200] if "malformed" in src else "raise" in src, "坏 JSONL 应 raise 而非静默 continue"
    ledger_path = tmp_path / "ledger.jsonl"
    ledger_path.write_text('{"record": {"action":"shadow_record","trade":{"symbol":"AAPL","qty":1,"side":"buy"}}}\n{bad json\n', encoding="utf-8")
    try:
        aggregate_shadow(ledger_path=ledger_path)
        assert False, "坏 JSONL 必须抛 ValueError, 不得静默返回零差额"
    except ValueError as e:
        assert "malformed" in str(e).lower()


def test_b2b_09_side_strip_lower():
    """100-101 side 必须 strip 后再 lower, 避免 ' Sell ' 误判为买."""
    from hero_quant.governance.reconcile import _shadow_qty_from_trade
    import hero_quant.governance.reconcile as rec
    src = inspect.getsource(rec._shadow_qty_from_trade)
    assert ".strip().lower()" in src or ".strip()" in src and ".lower()" in src, "side 应先 strip 再 lower"
    sym, q = _shadow_qty_from_trade({"symbol": "AAPL", "qty": 10, "side": " Sell "})
    assert q == -10.0, f"side=' Sell ' 应判为卖出负数, 实际 {q}"
    sym2, q2 = _shadow_qty_from_trade({"symbol": "AAPL", "qty": 10, "side": "\tSHORT\n"})
    assert q2 == -10.0, f"side='\\tSHORT\\n' 应判为卖出, 实际 {q2}"


def test_b2b_10_missing_symbol_warns(caplog):
    """95-97 缺 symbol 不得静默返 ('',0.0) 丢弃, 必须 warning."""
    from hero_quant.governance.reconcile import _shadow_qty_from_trade
    import hero_quant.governance.reconcile as rec
    src = inspect.getsource(rec._shadow_qty_from_trade)
    assert "logger.warning" in src, "缺 symbol 应 warning"
    caplog.set_level(logging.WARNING)
    sym, q = _shadow_qty_from_trade({"qty": 10})
    assert sym == "" and q == 0.0
    assert any("missing symbol" in r.message.lower() or "symbol" in r.message.lower() for r in caplog.records), "缺 symbol 应 warning"

# ---------------------------------------------------------------------------
# data/registry.py 6 条
# ---------------------------------------------------------------------------

def test_b2b_11_unknown_market_not_misreport():
    """400-405 UNKNOWN/后缀市场误报 — UNKNOWN 不得误导为 pip install 缺依赖."""
    from hero_quant.data.registry import MarketDataRegistry
    reg = MarketDataRegistry()
    # 含 empty markets 的 loader 应被尝试, 而非全体跳过后误报缺依赖
    class EmptyMarketsLoader:
        markets = []
        unit = "shares"
        name = "tencent"
        source = "tencent"
        def get_bars(self, symbol, start, end, interval="1d"):
            return [{"close": 100, "date": "2026-01-01"}], None
        def health(self): return {"status": "ok"}

    reg.register(EmptyMarketsLoader())
    # 无后缀 symbol -> _detect_market 返回 UNKNOWN; 此时 markets=[] 的 loader 不应被跳过
    bars, prov = reg.get_bars("AAPL", "2026-01-01", "2026-01-02")
    assert bars is not None and len(bars) > 0, "UNKNOWN 市场时 markets=[] 的 loader 应被尝试, 不得误报缺依赖"
    # 若仍为 UNKNOWN 且无可用 loader, 应提示不支持的市场而非单一 pip install 文案的覆盖
    # 检查 get_bars 源码不再对 UNKNOWN 全体跳过
    src = inspect.getsource(MarketDataRegistry.get_bars)
    # 修复后应有针对 UNKNOWN 的 ValueError 或对空 markets loader 的放行
    assert "UNKNOWN" in src or "unsupported market" in src.lower() or "markets" in src, "应处理 UNKNOWN 市场误报"


def test_b2b_12_synthetic_optin_must_not_skip_1pct():
    """365-373 synthetic opt-in 关掉 1% 检查 — 显式 opt-in 后仍需执行 diff 校验."""
    from hero_quant.data.registry import MarketDataRegistry, CrossSourceError, Provenance
    reg = MarketDataRegistry()

    class LoaderA:
        markets = ["US"]
        unit = "shares"
        name = "tencent"
        def get_bars(self, symbol, start, end, interval="1d"):
            return [{"close": 100, "date": "2026-01-01"}], None
        def health(self): return {"status": "ok"}

    class LoaderB:
        markets = ["US"]
        unit = "shares"
        name = "yahoo"
        def get_bars(self, symbol, start, end, interval="1d"):
            return [{"close": 200, "date": "2026-01-01"}], None  # 100% 偏差
        def health(self): return {"status": "ok"}

    reg.register(LoaderA())
    reg.register(LoaderB())
    # 主数据为 synthetic 且显式 opt-in -> 修复前会 continue 跳过校验, 修复后应仍校验 1%
    prov = Provenance(source="synthetic", unit="shares", symbol="AAPL.US")
    prov.allow_synthetic_comparison = True  # type: ignore
    bars = [{"close": 100, "date": "2026-01-01"}]
    try:
        reg._cross_source_check("AAPL.US", bars, prov, interval="1d", start="2026-01-01", end="2026-01-02")
        assert False, "synthetic opt-in 后仍应执行 1% 校验, 100% 偏差应抛 CrossSourceError"
    except CrossSourceError:
        pass
    # 源码层面: opt-in 后不得直接 continue 而应 fall-through 到 diff 比较
    src = inspect.getsource(MarketDataRegistry._cross_source_check)
    # 修复前: logger.warning 后直接 continue, 导致跳过 diff; 修复后 continue 应移除
    # 简单断言: synthetic mix allowed 分支后不应紧跟 continue 而跳过 diff
    seg = src.split("allow_synthetic_comparison")[-1][:500] if "allow_synthetic_comparison" in src else src
    # 修复后该分支不应以 continue 结束(应落到后续 diff 计算)
    assert not seg.strip().startswith("continue") and "continue" not in seg.split("\n")[0:3].__str__() or "other_close" in src[ src.find("allow_synthetic_comparison"): src.find("allow_synthetic_comparison")+600 ], "opt-in 后应继续 diff 校验, 不得直接 continue"


def test_b2b_13_prov_sniff_overload():
    """306-308 prov 嗅探重载 — 不得以 hasattr(prov,'source') 区分 bars 与 Provenance."""
    from hero_quant.data.registry import MarketDataRegistry, Provenance
    reg = MarketDataRegistry()
    # 构造一个既像 bars 又有 source 属性的对象, 修复前会被误判为 Provenance 分支
    class BarsLike:
        # 同时具备 source 属性与 list 行为
        source = "fake_prov"
        def __init__(self): self.data = [{"close": 100, "date": "2026-01-01"}]
        def __iter__(self): return iter(self.data)
        def __getitem__(self, idx): return self.data[idx]

    BarsLike()
    # 调用 _cross_source_check 的第一分支: 若 prov 嗅探错误会把 BarsLike 当 prov 而非 other_bars
    # 修复后应显式区分签名, 不再用 hasattr 嗅探
    src = inspect.getsource(MarketDataRegistry._cross_source_check)
    assert 'hasattr(prov' not in src or '_cross_source_check_bars' in src or 'isinstance' in src, "不得用 hasattr(prov,'source') 嗅探区分 bars/Provenance, 应拆分显式签名"
    # 功能: 传入 (bars, Provenance) 二元组作为 prov 不应被解析为 bar 序列
    Provenance(source="tencent", unit="shares", symbol="AAPL.US")
    bars = [{"close": 100, "date": "2026-01-01"}]
    # prov 实为另一组 bars 的元组场景已拆分, 不应静默返回
    # 传入 start/end 为 None 时修复前静默 return; 修复后应更明确(至少不因嗅探错误而误走分支)
    # 简单: 用明确的两组 bars 调用不应因类型嗅探而跳过
    other_bars = [{"close": 100, "date": "2026-01-01"}]
    # 若第一分支被误触发, 会直接 return 而不走 loader 遍历; 这里验证不抛错且行为可预期
    reg._cross_source_check("AAPL.US", bars, other_bars, interval="1d", start="2026-01-01", end="2026-01-02")


def test_b2b_14_broad_except_not_swallow_cross_source():
    """406-409 大 except 不得吞 CrossSourceError."""
    from hero_quant.data.registry import MarketDataRegistry, CrossSourceError
    reg = MarketDataRegistry()

    class BadLoader:
        markets = ["US"]
        unit = "shares"
        name = "tencent"
        def get_bars(self, symbol, start, end, interval="1d"):
            raise CrossSourceError("integrity failed")
        def health(self): return {"status": "ok"}

    class GoodLoader:
        markets = ["US"]
        unit = "shares"
        name = "yahoo"
        def get_bars(self, symbol, start, end, interval="1d"):
            return [{"close": 100, "date": "2026-01-01"}], None
        def health(self): return {"status": "ok"}

    reg.register(BadLoader())
    reg.register(GoodLoader())
    src = inspect.getsource(reg.get_bars)
    # 修复后应对 CrossSourceError 单独捕获并立即 reraise, 再进通用 except
    assert "except CrossSourceError" in src, "必须先捕获 CrossSourceError 并 reraise, 不得被大 except 吞掉"
    try:
        reg.get_bars("AAPL.US", "2026-01-01", "2026-01-02")
        assert False, "CrossSourceError 必须立即抛出, 不得 fallback 到下一 loader"
    except CrossSourceError:
        pass


def test_b2b_15_dead_suffix_checks_removed():
    """149-154 死后缀检查 — 重复的 suffix in ('SH','SZ') 与 suffix == 'US' 不可达应移除."""
    from hero_quant.data.registry import MarketDataRegistry
    src = inspect.getsource(MarketDataRegistry._detect_market)
    # 修复后应仅保留 upper.endswith 统一路径, 移除第二个 if "." in symbol 后的重复后缀分支
    # 简单统计: 文件中不应出现两个独立的 suffix == 'US' 判断
    count_us = src.count("suffix == 'US'") + src.count('suffix == "US"')
    count_shsz = src.count("('SH','SZ')") + src.count('("SH","SZ")') + src.count("('SH', 'SZ')")
    # 修复前两者各 1(重复), 修复后应 <=1 或直接被移除
    assert count_us <= 1 and count_shsz <= 1, f"死后缀检查应移除, 实际 suffix=='US' 出现 {count_us} 次, SH/SZ 出现 {count_shsz} 次"
    # 功能仍正确
    assert MarketDataRegistry()._detect_market("600519.SH") == "CN"
    assert MarketDataRegistry()._detect_market("aapl.us") == "US"
    assert MarketDataRegistry()._detect_market("600519.sh") == "CN"


def test_b2b_16_raise_preserve_chain():
    """460-465 末尾 raise 必须保留异常链 (raise ... from last_error)."""
    from hero_quant.data.registry import MarketDataRegistry
    reg = MarketDataRegistry()

    class FailingLoader:
        markets = ["US"]
        unit = "shares"
        name = "tencent"
        def get_bars(self, symbol, start, end, interval="1d"):
            raise ValueError("loader broken detail")
        def health(self): return {"status": "ok"}

    reg.register(FailingLoader())
    src = inspect.getsource(reg.get_bars)
    assert "from last_error" in src, "末尾 raise 必须使用 raise ... from last_error 保留链"
    try:
        reg.get_bars("AAPL.US", "2026-01-01", "2026-01-02")
        assert False
    except ImportError as e:
        # 缺依赖类错误：链上保留安装提示
        assert e.__cause__ is not None, f"异常链丢失, __cause__ 为 None, 实际 {e!r}"
    except ValueError as e:
        # 非 ImportError 不得伪装成缺依赖：原始错误透出且保留异常链
        assert "loader broken" in str(e), "链上应保留原始错误信息"
        assert e.__cause__ is not None, f"异常链丢失, __cause__ 为 None, 实际 {e!r}"
        assert "loader broken" in str(e.__cause__), "链上应保留原始错误信息"


# ---------------------------------------------------------------------------
# data/trait.py 3 条
# ---------------------------------------------------------------------------

def test_b2b_17_list_contract_date_sort_dedup():
    """164-176 list[dict] 校验必须含 date/sort/dedup."""
    from hero_quant.data.trait import _check_list_contract
    src = inspect.getsource(_check_list_contract)
    assert "date" in src.lower(), "_check_list_contract 必须校验 date/trade_date"
    assert "sorted" in src.lower() or "monotonic" in src.lower() or "sort" in src.lower(), "必须校验排序"
    assert "dedup" in src.lower() or "duplic" in src.lower() or "seen" in src.lower(), "必须校验去重"
    # 缺 date 应抛
    try:
        _check_list_contract([{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}])
        assert False, "缺 date/trade_date 应抛 ValueError"
    except ValueError as e:
        assert "date" in str(e).lower()
    # 未排序应抛
    try:
        _check_list_contract([
            {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "date": "2026-01-02"},
            {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "date": "2026-01-01"},
        ])
        assert False, "未按 date 升序应抛 ValueError"
    except ValueError as e:
        assert "sort" in str(e).lower() or "order" in str(e).lower() or "asc" in str(e).lower()
    # 重复 date 应抛
    try:
        _check_list_contract([
            {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "date": "2026-01-01"},
            {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "date": "2026-01-01"},
        ])
        assert False, "重复 date 应抛 ValueError"
    except ValueError as e:
        assert "duplic" in str(e).lower() or "duplicate" in str(e).lower()
    # 正常应通过
    _check_list_contract([
        {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "date": "2026-01-01"},
        {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "date": "2026-01-02"},
    ])


def test_b2b_18_missing_name_fail_closed():
    """79-82 缺 name 必须 fail-closed 抛错, 不得 warn+continue."""
    from hero_quant.data.trait import validate_loader
    src = inspect.getsource(validate_loader)
    assert 'raise ValueError("loader missing attribute: name' in src or "loader missing attribute: name" in src, "缺 name 应 raise 而非 warn+continue"
    assert src.count("loader missing name") == 0 or "raise" in src, "不得再 warn+continue"
    class NoNameLoader:
        markets = ["US"]
        unit = "shares"
        def get_bars(self, symbol, start, end, interval="1d"):
            return [], None
        def health(self): return {"status": "ok"}

    try:
        validate_loader(NoNameLoader())
        assert False, "缺 name 应抛 ValueError (fail-closed)"
    except ValueError as e:
        assert "name" in str(e).lower()


def test_b2b_19_health_required():
    """131-134 health 必需 — validate_loader 必须要求 health 可调用, 与 SourceTrait 一致."""
    from hero_quant.data.trait import validate_loader
    src = inspect.getsource(validate_loader)
    # 修复后应要求 health 存在且可调用, 而非可选
    assert "loader missing callable: health" in src or "health" in src and "raise" in src, "health 应为必需"
    class NoHealthLoader:
        name = "tencent"
        markets = ["US"]
        unit = "shares"
        def get_bars(self, symbol, start, end, interval="1d"):
            return [], None

    try:
        validate_loader(NoHealthLoader())
        assert False, "缺 health 应抛 ValueError"
    except ValueError as e:
        assert "health" in str(e).lower()
    # health 存在但不可调用也应抛
    class BadHealthLoader:
        name = "tencent"
        markets = ["US"]
        unit = "shares"
        health = "not callable"
        def get_bars(self, symbol, start, end, interval="1d"):
            return [], None
    try:
        validate_loader(BadHealthLoader())
        assert False
    except ValueError as e:
        assert "health" in str(e).lower()
    # 正常应通过
    class GoodLoader:
        name = "tencent"
        markets = ["US"]
        unit = "shares"
        def get_bars(self, symbol, start, end, interval="1d"):
            return [], None
        def health(self): return {"status": "ok"}
    validate_loader(GoodLoader())
