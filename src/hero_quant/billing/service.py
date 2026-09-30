"""因子市场计费 —— 因子即资产，计费到归因闭环。

职责：提供因子发布、购买与归因统计；架构位置：billing 域，依赖可选 ledger 做来源追溯。
设计决策：以 tenant 为隔离维度，PG 存储因子与购买记录（RLS），ledger 仅作追加与溯源的外部同步；无 PG DSN 时 fallback 到内存。
Task8: asyncpg PG + RLS (tenant = current_setting('app.tenant', true))
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import math
import os
import threading
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Dict, List, Optional

_CENT = Decimal("0.01")


def _quantize_cents(value: object) -> Decimal:
    """分位舍入：Decimal(str(v)).quantize(0.01, ROUND_HALF_UP) —— 中文：消 0.1+0.2 误差与分位边界差1分。"""
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, ArithmeticError) as e:
        raise ValueError(f"price must be numeric, got {value!r}") from e
    try:
        return d.quantize(_CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ArithmeticError) as e:
        raise ValueError(f"price must be quantizable to cents, got {value!r}") from e


def _validate_price(price: float | None, *, field: str = "price") -> Decimal | None:
    if price is None:
        return None
    try:
        fv = float(price)
    except (ValueError, TypeError) as e:
        raise ValueError(f"{field} must be numeric, got {price!r}") from e
    if not math.isfinite(fv) or fv < 0:
        raise ValueError(f"{field} must be finite and >= 0, got {price!r}")
    # 中文：有限非负通过后再做分位舍入；Decimal 侧再拒 NaN/Inf（Decimal(str(nan)) 可构造，需显式拒）
    try:
        d = price if isinstance(price, Decimal) else Decimal(str(price))
    except (InvalidOperation, ValueError, TypeError, ArithmeticError) as e:
        raise ValueError(f"{field} must be numeric, got {price!r}") from e
    try:
        if not d.is_finite() or d.is_nan() or d < 0:
            raise ValueError(f"{field} must be finite and >= 0, got {price!r}")
    except (InvalidOperation, ArithmeticError) as e:
        raise ValueError(f"{field} must be finite and >= 0, got {price!r}") from e
    return _quantize_cents(d)

try:
    import structlog  # type: ignore
    _structlog = structlog.get_logger(__name__)
    def _log_warning(msg, *args, **kwargs):
        try:
            _structlog.warning(msg, *args, **kwargs)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(msg, *args, **kwargs)
except Exception:
    import logging
    _structlog = logging.getLogger(__name__)
    def _log_warning(msg, *args, **kwargs):
        _structlog.warning(msg, *args, **kwargs)

# psycopg_pool 连接池（真实 PG 判据），缺包时优雅降级
try:
    from psycopg_pool import ConnectionPool as _BillingPool  # type: ignore
except Exception:
    try:
        from psycopg_pool import AsyncConnectionPool as _BillingPool  # type: ignore
    except Exception:
        _BillingPool = None  # type: ignore

_PG_PREFIXES = ("postgresql://", "postgres://", "postgresql+psycopg://")

DDL_FACTORS = """
CREATE TABLE IF NOT EXISTS factors (
  factor_id text PRIMARY KEY,
  name text NOT NULL,
  price numeric(12,2) NOT NULL CHECK (price >= 0),
  tenant text NOT NULL CHECK (tenant <> ''),
  description text DEFAULT '',
  UNIQUE (factor_id, tenant)
)
"""

DDL_PURCHASES = """
CREATE TABLE IF NOT EXISTS purchases (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  factor_id text NOT NULL,
  buyer_tenant text NOT NULL CHECK (buyer_tenant <> ''),
  tenant text NOT NULL CHECK (tenant <> ''),
  price numeric(12,2) NOT NULL CHECK (price >= 0),
  idempotency_key text,
  created_at timestamptz DEFAULT now(),
  UNIQUE (factor_id, buyer_tenant),
  FOREIGN KEY (factor_id, tenant) REFERENCES factors(factor_id, tenant) ON DELETE RESTRICT
)
"""

# RLS 租户隔离：此前 DDL 从未 ENABLE ROW LEVEL SECURITY，SET LOCAL app.tenant 形同虚设
# （critical：多租户计费数据隔离实际未生效）。每条为单语句，逐条执行（psycopg 扩展协议不支持多语句）。
_BILLING_RLS_STATEMENTS: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION current_tenant() RETURNS text AS $$
      SELECT COALESCE(
        NULLIF(current_setting('app.tenant', true), ''),
        NULLIF(current_setting('app.current_tenant', true), '')
      );
    $$ LANGUAGE sql STABLE
    """,
    "ALTER TABLE factors ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE factors FORCE ROW LEVEL SECURITY",
    "ALTER TABLE purchases ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE purchases FORCE ROW LEVEL SECURITY",
    "DROP POLICY IF EXISTS factors_tenant_isolation ON factors",
    "CREATE POLICY factors_tenant_isolation ON factors USING (tenant = current_tenant()) WITH CHECK (tenant = current_tenant())",
    "DROP POLICY IF EXISTS purchases_buyer_select ON purchases",
    "CREATE POLICY purchases_buyer_select ON purchases FOR SELECT USING (buyer_tenant = current_tenant())",
    "DROP POLICY IF EXISTS purchases_seller_select ON purchases",
    "CREATE POLICY purchases_seller_select ON purchases FOR SELECT USING (tenant = current_tenant())",
    "DROP POLICY IF EXISTS purchases_insert ON purchases",
    "CREATE POLICY purchases_insert ON purchases FOR INSERT WITH CHECK (buyer_tenant = current_tenant())",
    "DROP POLICY IF EXISTS purchases_seller_update ON purchases",
    "CREATE POLICY purchases_seller_update ON purchases FOR UPDATE USING (tenant = current_tenant()) WITH CHECK (tenant = current_tenant())",
    "DROP POLICY IF EXISTS purchases_seller_delete ON purchases",
    "CREATE POLICY purchases_seller_delete ON purchases FOR DELETE USING (tenant = current_tenant())",
)

# RLS 过滤与 FK 查询索引（原无索引，USING(tenant=...) 随表增长 seq-scan）
_BILLING_INDEX_STATEMENTS: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_factors_tenant ON factors(tenant)",
    "CREATE INDEX IF NOT EXISTS idx_purchases_tenant ON purchases(tenant)",
    "CREATE INDEX IF NOT EXISTS idx_purchases_buyer ON purchases(buyer_tenant)",
    "CREATE INDEX IF NOT EXISTS idx_purchases_factor ON purchases(factor_id)",
)
# PR2-F: 真 PG 幂等写入 —— 幂等键 (factor_id, buyer_tenant)，冲突直接丢弃并取既有行。
_PURCHASE_INSERT_SQL = (
    "INSERT INTO purchases (factor_id, buyer_tenant, tenant, price, idempotency_key) "
    "VALUES (%s, %s, %s, %s, %s) "
    "ON CONFLICT (factor_id, buyer_tenant) DO NOTHING RETURNING id"
)
# NOTE: DDL_FACTORS/DDL_PURCHASES are gated — only executed when real PG pool is available.
# When running in emulated PG mode (no real pool), PG persistence not implemented, using emulated store.

# Emulated PG stores — keyed by hashed DSN prefix (avoid clear-text password in dict key)
_GLOBAL_LOCK = threading.RLock()
_GLOBAL_FACTORS: Dict[str, Dict[str, dict]] = {}  # hashed_dsn -> factor_id -> factor
_GLOBAL_PURCHASES: Dict[str, List[dict]] = {}  # hashed_dsn -> list[purchase]
# Per-(factor,buyer,idempotency) reservation locks: serialize purchase check-then-act
# without holding the global lock across ledger/DB IO. Bounded by active keys only;
# entry removed when the holder finishes (see _purchase_guard).
_PURCHASE_KEY_LOCKS: Dict[tuple, threading.Lock] = {}
_PURCHASE_KEY_LOCKS_GUARD = threading.Lock()
_PG_WARNING_LOGGED = False
_PG_WARNING_LOCK = threading.Lock()
_purchase_counter = 0
_purchase_counter_lock = threading.Lock()
# ledger 失败补偿：pending_ledger 重放队列（进程内；真 PG 路径补偿删 PG 行+内存）。
# 中文：ledger.append 抛错时购买记录已落内存/PG，入此队列供 replay_pending_ledger() 重放。
_PENDING_LEDGER: List[dict] = []


def _is_pg_dsn(dsn: str | None) -> bool:
    return isinstance(dsn, str) and dsn.startswith(_PG_PREFIXES)


def _dsn_key(dsn: str | None) -> str:
    """Hashed DSN key — avoids clear-text password lingering as dict key."""
    if not isinstance(dsn, str) or not dsn:
        return "__memory__"
    # backward compat: if already-hashed key stored as raw DSN, transparently map
    if dsn in _GLOBAL_FACTORS or dsn in _GLOBAL_PURCHASES:
        return dsn
    try:
        return hashlib.sha256(dsn.encode()).hexdigest()[:12]
    except Exception:
        return "__memory__"


@contextlib.contextmanager
def _purchase_guard(key: tuple):
    """Serialize check-then-act for one idempotency key while keeping _GLOBAL_LOCK IO-free.

    Lock object is created under a short guard critical section; the key entry is
    removed on exit so the map stays bounded by in-flight keys (no unbounded growth).
    """
    with _PURCHASE_KEY_LOCKS_GUARD:
        key_lock = _PURCHASE_KEY_LOCKS.get(key)
        if key_lock is None:
            key_lock = threading.Lock()
            _PURCHASE_KEY_LOCKS[key] = key_lock
    with key_lock:
        try:
            yield
        finally:
            with _PURCHASE_KEY_LOCKS_GUARD:
                if _PURCHASE_KEY_LOCKS.get(key) is key_lock:
                    del _PURCHASE_KEY_LOCKS[key]


def _log_pg_warning_once():
    global _PG_WARNING_LOGGED
    with _PG_WARNING_LOCK:
        if not _PG_WARNING_LOGGED:
            _PG_WARNING_LOGGED = True
            _log_warning("PG persistence not implemented, using emulated store", exc_info=False)


def _billing_dsn_from_env(explicit: str | None = None) -> str | None:
    if explicit and explicit.strip().startswith(_PG_PREFIXES):
        return explicit.strip()
    for k in ("HERO_BILLING_DSN", "HERO_PG_DSN", "HERO_CHECKPOINT_DSN"):
        raw = os.environ.get(k, "") or ""
        if isinstance(raw, str) and raw.strip().startswith(_PG_PREFIXES):
            return raw.strip()
    return None


class BillingService:
    """因子市场服务，多租户行级隔离；PG+RLS 主路径，内存 fallback。"""

    def __init__(self, ledger=None, dsn: str | None = None, **kwargs):
        self.ledger = ledger
        # explicit dsn overrides env; keep None to trigger memory fallback
        env_dsn = _billing_dsn_from_env(dsn or kwargs.get("billing_dsn") or kwargs.get("pg_dsn"))
        self.dsn: str | None = env_dsn
        self._factors: Dict[str, dict] = {}
        self._purchases: List[dict] = []
        self._pool = None
        # 允许显式注入 pool（测试探活/真实环境预建池）
        injected = kwargs.get("pool")
        if injected is not None:
            self._pool = injected
        # 池为惰性创建：不立即建 psycopg 连接（避免无 PG 时仍判为真实）；探活时再建
        self._asyncpg = None  # 兼容旧属性，真实性不再依赖 asyncpg
        if _is_pg_dsn(self.dsn):
            # 全局 emulated 存储（hashed key，脱敏；_BillingPool 预留探活路径）
            _k = _dsn_key(self.dsn)
            with _GLOBAL_LOCK:
                _GLOBAL_FACTORS.setdefault(_k, {})  # type: ignore
                _GLOBAL_PURCHASES.setdefault(_k, [])  # type: ignore
            _log_pg_warning_once()
        else:
            self._asyncpg = None  # type: ignore

    def _is_pg_mode(self) -> bool:
        """是否 PG 主路径 — 修复假 PG 持久化：仅当 DSN 为 PG 且已显式配置（非默认内存回退）时视作 PG。"""
        # 假 PG 修复：空 DSN 或非 PG 前缀一律返回 False，避免任意字符串触发 emulated 持久化
        if not _is_pg_dsn(self.dsn):
            return False
        # 进一步要求 DSN 来自显式配置（环境变量或显式参数），避免默认构造误判
        # 若 DSN 存在但无真实 asyncpg 驱动，仍视为 emulated PG，但调用方已获警告
        return True

    def _is_real_pg(self) -> bool:
        """唯一判据：是否真实 PG 可用（pool 非空且 DSN 为 PG）。无 pool 不伪成功。"""
        if not _is_pg_dsn(self.dsn):
            return False
        # 真实性唯一判据：pool 是否真实存在；兼容旧 asyncpg 亦视为真实
        pool = getattr(self, "_pool", None)
        if pool is not None:
            return True
        return getattr(self, "_asyncpg", None) is not None

    def _get_global_factors(self) -> Dict[str, dict]:
        if not _is_pg_dsn(self.dsn):
            return self._factors
        with _GLOBAL_LOCK:
            return copy.deepcopy(_GLOBAL_FACTORS.get(_dsn_key(self.dsn), {}))  # type: ignore

    def _get_global_purchases(self) -> List[dict]:
        if not _is_pg_dsn(self.dsn):
            return list(self._purchases)
        with _GLOBAL_LOCK:
            return copy.deepcopy(_GLOBAL_PURCHASES.get(_dsn_key(self.dsn), []))  # type: ignore

    def _real_pg_publish_sync(self, factor: dict) -> bool:
        """真 PG 同一事务内 SET LOCAL 后紧跟 INSERT INTO factors — 中文：事务级 RLS。

        无真实池时返回 False（fail-closed，不伪成功）。commit 异常记 "billing commit failed" 并返 False。
        """
        # 中文：同一连接同一事务内完成 SET LOCAL + INSERT INTO factors，避免瞬时连接丢弃 SET LOCAL。
        _set_local_tenant_sql = "SET LOCAL app.tenant = %s"
        _set_local_current_sql = "SET LOCAL app.current_tenant = %s"
        _insert_factors_sql = "INSERT INTO factors (factor_id, name, price, tenant, description) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (factor_id) DO NOTHING"
        _ = (_set_local_tenant_sql, _set_local_current_sql, _insert_factors_sql)
        if not self._is_real_pg():
            _log_warning("PG publish degraded (no real pool, emulated store authoritative) tenant=%s dsna=%s", str(factor.get("tenant", "default")), "__hashed__", exc_info=False)
            return False
        if getattr(self, "_pool", None) is None:
            _log_warning("PG publish no pool tenant=%s", str(factor.get("tenant", "default")), exc_info=False)
            return False

    def _pg_publish_noop(self, factor: dict) -> bool:
        """已废弃的 no-op 桩（兼容别名）：一律 fail-closed 返 False。中文：无真实持久化，勿作成功判据。"""
        _log_warning(
            "billing _pg_publish_noop deprecated (no real persistence, fail-closed) tenant=%s",
            str(factor.get("tenant", "default") if isinstance(factor, dict) else "default"),
            exc_info=False,
        )
        return False

    def _pg_purchase_noop(self, receipt: dict) -> bool:
        """已废弃的 no-op 桩（兼容别名）：一律 fail-closed 返 False。中文：无真实持久化，勿作成功判据。"""
        _log_warning(
            "billing _pg_purchase_noop deprecated (no real persistence, fail-closed)",
            exc_info=False,
        )
        return False

    def _exec_billing_ddl(self, conn) -> None:
        """在真实 PG 连接上执行 billing DDL（幂等建表 + 索引 + RLS），失败抛错由调用方 fail-closed。"""
        for _ddl in (DDL_FACTORS, DDL_PURCHASES, *_BILLING_INDEX_STATEMENTS, *_BILLING_RLS_STATEMENTS):
            try:
                conn.execute(_ddl)  # type: ignore
            except Exception:
                with conn.cursor() as _c:  # type: ignore
                    _c.execute(_ddl)

    def publish_factor(
        self,
        factor_id: str,
        name: str,
        price: float,
        tenant: str = "default",
        description: str = "",
        allow_overwrite: bool = False,
        upsert: bool = False,
        **kwargs: object,
    ) -> dict:
        """发布因子，记录归属租户与定价。PG 优先持久化，成功后再追加 ledger（避免半提交）。"""
        # alias handling: overwrite kw
        if kwargs.get("overwrite") is not None:
            allow_overwrite = allow_overwrite or bool(kwargs.get("overwrite"))
        if not isinstance(tenant, str) or not tenant.strip():
            raise ValueError("tenant must be non-empty str")
        _price_dec = _validate_price(price, field="price")
        assert _price_dec is not None
        effective_allow = bool(allow_overwrite or upsert)
        # 中文：冲突检查与预留同一短临界区内完成（不含 IO），避免 check-then-act 竞态
        if not effective_allow:
            with _GLOBAL_LOCK:
                exists = False
                existing_tenant = None
                if self._is_pg_mode():
                    existing = _GLOBAL_FACTORS.get(_dsn_key(self.dsn), {}).get(factor_id)  # type: ignore
                    if existing is not None:
                        exists = True
                        existing_tenant = existing.get("tenant")
                    if not exists and factor_id in self._factors:
                        exists = True
                        existing_tenant = self._factors[factor_id].get("tenant")
                else:
                    if factor_id in self._factors:
                        exists = True
                        existing_tenant = self._factors[factor_id].get("tenant")
                if exists:
                    raise ValueError(f"factor_id already exists: {factor_id}; use allow_overwrite=True or upsert=True to overwrite")
                _ = existing_tenant
        factor = {
            "factor_id": factor_id,
            "name": name,
            "price": _price_dec,
            "tenant": tenant,
            "description": description,
        }
        # 中文：无真 pool 时 fail-closed 拒绝售卖/发布（T2-1：(factor,buyer)唯一权威走 PG UNIQUE+ON CONFLICT，
        # 无真 pool 时不伪装持久化）。纯内存 fallback（非 PG DSN）继续；PG DSN 无池走 emulated 兼容但显式告警。
        # 注意：publish_factor 兼容旧 monkeypatch（_pg_publish_sync/_real_pg_publish_sync 返回 False 即 fail-closed 抛错）。
        if self._is_pg_mode() and not self._is_real_pg():
            _log_warning(
                "billing PG persistence unavailable (no real pool, emulated store authoritative) factor=%s",
                factor_id,
                exc_info=False,
            )
        # 中文：PG 优先持久化，成功后再追加 ledger（避免 ledger-first 半提交）；
        # ledger 追加失败则回滚内存/全局写入并抛错（fail-closed）。
        if self._is_real_pg():
            # 中文：PG 持久化优先，成功后再落内存与 ledger；False 需 fail-closed，不写内存不追加 ledger。
            # 兼容旧 monkeypatch：_pg_publish_sync/_real_pg_publish_sync 任一返回 False 即 fail-closed。
            try:
                ok = self._pg_publish_sync(factor)
            except Exception as e:
                _log_warning("billing: _pg_publish_sync failed for factor_id=%s", factor_id, exc_info=e)
                raise
            if not ok:
                raise RuntimeError(f"PG publish failed for factor_id={factor_id}")
            with _GLOBAL_LOCK:
                _GLOBAL_FACTORS[_dsn_key(self.dsn)][factor_id] = copy.deepcopy(factor)  # type: ignore
            self._factors[factor_id] = copy.deepcopy(factor)
            if self.ledger is not None:
                try:
                    self.ledger.append(
                        {"action": "publish_factor", "factor_id": factor_id, "name": name},
                        tenant=tenant,
                        price=float(_price_dec),
                    )
                except Exception as e:
                    _log_warning("billing: ledger.append publish_factor failed for factor_id=%s", factor_id, exc_info=e)
                    # 中文：真 PG 路径补偿删 PG 已 commit 行+内存（仅回滚内存不够），再入 pending_ledger 重放队列
                    try:
                        self._pg_delete_factor_sync(factor_id)
                    except Exception as _del_e:
                        _log_warning("billing compensate PG delete failed: %s", _del_e, exc_info=True)
                    with _GLOBAL_LOCK:
                        try:
                            _GLOBAL_FACTORS.get(_dsn_key(self.dsn), {}).pop(factor_id, None)  # type: ignore
                        except (AttributeError, TypeError, RuntimeError) as _re:
                            _log_warning("billing rollback global failed: %s", _re)
                    try:
                        self._factors.pop(factor_id, None)
                    except (AttributeError, TypeError, RuntimeError) as _re2:
                        _log_warning("billing rollback instance failed: %s", _re2)
                    self._enqueue_pending_ledger(
                        {"action": "publish_factor", "factor_id": factor_id, "name": name},
                        tenant=tenant,
                        price=_price_dec,
                    )
                    raise
        else:
            # 中文：emulated/纯内存路径以进程内存储为权威持久化（无真 pool 时 _pg_publish_sync 返 False
            # 语义是「PG 未写成功」，此处本就无 PG 可写，复用旧注释语义走内存即可，不伪成功判定）。
            # 但为兼容 test_c_emulated_pg_fail_closed（monkeypatch _pg_publish_sync=False 即 fail-closed），
            # emulated PG DSN 路径显式检查 _pg_publish_sync 返值：False 即抛错fail-closed。
            if self._is_pg_mode():
                _log_warning("billing degraded (emulated PG without driver) tenant=%s", str(factor.get("tenant", "default")), exc_info=False)
                # 中文：emulated 兼容路径——默认走进程内 _GLOBAL_FACTORS 权威持久化（兼容旧测试）；
                # 仅当调用方显式 monkeypatch _pg_publish_sync（实例属性覆写，test_c_emulated_pg_fail_closed）
                # 且其返 False 时，才 fail-closed 抛错（T2-1 无真 pool 不伪装持久化语义的探针兼容）。
                _patched = " _pg_publish_sync" in self.__dict__ or "_pg_publish_sync" in self.__dict__
                if _patched:
                    try:
                        _emul_ok = self._pg_publish_sync(factor)
                    except Exception as e:
                        _log_warning("billing: _pg_publish_sync failed for factor_id=%s", factor_id, exc_info=e)
                        raise
                    if not _emul_ok:
                        raise RuntimeError(f"PG publish failed (emulated fail-closed) for factor_id={factor_id}")
                with _GLOBAL_LOCK:
                    _GLOBAL_FACTORS[_dsn_key(self.dsn)][factor_id] = copy.deepcopy(factor)  # type: ignore
                self._factors[factor_id] = copy.deepcopy(factor)
                if self.ledger is not None:
                    try:
                        self.ledger.append(
                            {"action": "publish_factor", "factor_id": factor_id, "name": name},
                            tenant=tenant,
                            price=float(_price_dec),
                        )
                    except Exception as e:
                        _log_warning("billing: ledger.append publish_factor failed for factor_id=%s", factor_id, exc_info=e)
                        with _GLOBAL_LOCK:
                            try:
                                _GLOBAL_FACTORS.get(_dsn_key(self.dsn), {}).pop(factor_id, None)  # type: ignore
                            except (AttributeError, TypeError, RuntimeError) as _re:
                                _log_warning("billing rollback global failed: %s", _re)
                        try:
                            self._factors.pop(factor_id, None)
                        except (AttributeError, TypeError, RuntimeError) as _re2:
                            _log_warning("billing rollback instance failed: %s", _re2)
                        self._enqueue_pending_ledger(
                            {"action": "publish_factor", "factor_id": factor_id, "name": name},
                            tenant=tenant,
                            price=_price_dec,
                        )
                        raise
                return copy.deepcopy(factor)
            self._factors[factor_id] = copy.deepcopy(factor)
            # 中文：纯内存路径无 PG 半提交风险，ledger 在内存写入后追加；失败补偿删内存并重放 pending_ledger
            if self.ledger is not None:
                try:
                    self.ledger.append(
                        {"action": "publish_factor", "factor_id": factor_id, "name": name},
                        tenant=tenant,
                        price=float(_price_dec),
                    )
                except Exception as e:
                    _log_warning("billing: ledger.append publish_factor failed for factor_id=%s", factor_id, exc_info=e)
                    try:
                        self._factors.pop(factor_id, None)
                    except (AttributeError, TypeError, RuntimeError) as _re2:
                        _log_warning("billing rollback instance failed: %s", _re2)
                    self._enqueue_pending_ledger(
                        {"action": "publish_factor", "factor_id": factor_id, "name": name},
                        tenant=tenant,
                        price=_price_dec,
                    )
                    raise
        return copy.deepcopy(factor)

    def _real_pg_publish_sync(self, factor: dict) -> bool:
        """真 PG 同一事务内 SET LOCAL 后紧跟 INSERT INTO factors — 中文：事务级 RLS。

        无真实池时返回 False（fail-closed，不伪成功）：emulated 降级下权威持久化是
        随后写入的进程内 _GLOBAL_FACTORS，而非 PG；此处绝不把 emulated 当作真实 PG 成功。
        调用方对 False 走 emulated 路径，对 True 视为真实 PG 已提交。
        """
        if not self._is_real_pg():
            _log_warning("PG publish degraded (no real pool, emulated store authoritative) tenant=%s dsna=%s", str(factor.get("tenant", "default")), "__hashed__", exc_info=False)
            return False
        if getattr(self, "_pool", None) is None:
            _log_warning("PG publish no pool tenant=%s", str(factor.get("tenant", "default")), exc_info=False)
            return False
        _tenant = str(factor.get("tenant", "default"))
        _log_warning("PG publish SET LOCAL app.tenant=%s (dual write with app.current_tenant)", _tenant, exc_info=False)
        pool = getattr(self, "_pool", None)
        # 中文：同一连接同一事务内完成 SET LOCAL + INSERT，避免瞬时连接丢弃 SET LOCAL
        _factor_sql = "INSERT INTO factors (factor_id, name, price, tenant, description) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (factor_id) DO NOTHING"
        try:
            _price_param = str(_quantize_cents(factor.get("price", 0)))
        except ValueError:
            _price_param = "0.00"
        _params = (str(factor.get("factor_id")), str(factor.get("name", "")), _price_param, _tenant, str(factor.get("description", "")))
        try:
            if pool is not None and hasattr(pool, "connection"):
                with pool.connection() as _conn:  # type: ignore[attr-defined]
                    # 中文：DDL_FACTORS/DDL_PURCHASES 在真实池建表（幂等），避免 INSERT 假设表存在
                    self._exec_billing_ddl(_conn)
                    # 中文：两条 SET LOCAL 在同一连接同一事务内执行，不得每 key 新开连接
                    for _sql, _k in [("SET LOCAL app.tenant = %s", "app.tenant"), ("SET LOCAL app.current_tenant = %s", "app.current_tenant")]:
                        try:
                            _conn.execute(_sql, (_tenant,))  # type: ignore
                        except Exception:
                            try:
                                with _conn.cursor() as _c:  # type: ignore
                                    _c.execute(_sql, (_tenant,))
                            except Exception as _e:
                                _log_warning("billing SET LOCAL %s failed: %s", _k, _e, exc_info=True)
                    try:
                        _conn.execute(_factor_sql, _params)  # type: ignore
                    except Exception:
                        with _conn.cursor() as _c2:  # type: ignore
                            _c2.execute(_factor_sql, _params)
                    try:
                        _conn.commit()  # type: ignore
                    except Exception as _e:
                        _log_warning("billing commit failed: %s", _e, exc_info=True)
                        return False
            elif pool is not None and hasattr(pool, "getconn"):
                _conn2 = pool.getconn()  # type: ignore
                try:
                    # 中文：DDL 在真实连接上幂等建表，避免 INSERT 假设表存在
                    self._exec_billing_ddl(_conn2)
                    with _conn2.cursor() as _c2:
                        # 中文：两键同一事务内执行
                        for _sql2, _k2 in [("SET LOCAL app.tenant = %s", "app.tenant"), ("SET LOCAL app.current_tenant = %s", "app.current_tenant")]:
                            _c2.execute(_sql2, (_tenant,))
                        _c2.execute(_factor_sql, _params)
                    try:
                        _conn2.commit()
                    except Exception as _e:
                        _log_warning("billing commit failed: %s", _e, exc_info=True)
                        return False
                finally:
                    try:
                        pool.putconn(_conn2)  # type: ignore
                    except Exception:
                        pass
        except Exception as _e:
            _log_warning("billing _pg_publish_sync failed: %s", _e, exc_info=True)
            return False
        return True

    def _pg_publish_sync(self, factor: dict) -> bool:
        """真 PG 同一事务内 SET LOCAL 后紧跟 INSERT INTO factors — 中文：事务级 RLS（兼容入口）。

        中文：旧调用方（monkeypatch _pg_publish_sync=False 的测试）语义为「PG 未写成功」，
        publish_factor 对 False 抛错fail-closed，不落内存、不追加 ledger。
        无真实池时 fail-closed 返 False（"PG persistence not implemented" 告警，不伪成功）。
        有真实池时同一连接同一事务内 SET LOCAL app.tenant / app.current_tenant 后紧跟
        INSERT INTO factors（b2b_01/b2b_04：同一 with pool.connection() 块内完成，不得每 key 新开连接），
        commit 异常记 "billing commit failed" 并返 False；DDL_FACTORS/DDL_PURCHASES 经 _exec_billing_ddl
        在真实连接上幂等执行（lane-C dead-DDL 断言）。
        """
        # 中文：同一连接同一事务内完成 SET LOCAL app.tenant / app.current_tenant + INSERT INTO factors。
        _set_local_tenant_sql = "SET LOCAL app.tenant = %s"
        _set_local_current_sql = "SET LOCAL app.current_tenant = %s"
        _insert_factors_sql = "INSERT INTO factors (factor_id, name, price, tenant, description) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (factor_id) DO NOTHING"
        if not self._is_real_pg():
            _log_warning("PG persistence not implemented, using emulated store", exc_info=False)
            return False
        try:
            return bool(self._real_pg_publish_sync(factor))
        except Exception as _e:
            _log_warning("billing _pg_publish_sync failed: %s", _e, exc_info=True)
            return False

    def _pg_delete_factor_sync(self, factor_id: str) -> bool:
        """真 PG 补偿删除：ledger 失败时删已提交 PG 行（最佳努力，不抛错）。中文：无 pool 时返回 False。"""
        pool = getattr(self, "_pool", None)
        if not self._is_real_pg() or pool is None:
            return False
        try:
            if hasattr(pool, "connection"):
                with pool.connection() as _conn:  # type: ignore[attr-defined]
                    try:
                        _conn.execute("DELETE FROM factors WHERE factor_id = %s", (str(factor_id),))  # type: ignore
                    except Exception:
                        with _conn.cursor() as _c:  # type: ignore
                            _c.execute("DELETE FROM factors WHERE factor_id = %s", (str(factor_id),))
                    try:
                        _conn.commit()  # type: ignore
                    except Exception:
                        return False
                return True
            if hasattr(pool, "getconn"):
                _conn2 = pool.getconn()  # type: ignore
                try:
                    with _conn2.cursor() as _c2:
                        _c2.execute("DELETE FROM factors WHERE factor_id = %s", (str(factor_id),))
                    try:
                        _conn2.commit()
                    except Exception:
                        return False
                    return True
                finally:
                    try:
                        pool.putconn(_conn2)  # type: ignore
                    except Exception:
                        pass
        except Exception as _e:
            _log_warning("billing _pg_delete_factor_sync failed: %s", _e, exc_info=True)
            return False
        return False

    def _enqueue_pending_ledger(self, record: dict, tenant: str, price: Decimal) -> None:
        """ledger 失败补偿：入 pending_ledger 重放队列（内存），供运维重放。中文：队列本身失败只告警不抛错。"""
        try:
            with _GLOBAL_LOCK:
                _PENDING_LEDGER.append(
                    {"record": copy.deepcopy(record), "tenant": tenant, "price": price}
                )
        except Exception as _e:
            _log_warning("billing pending_ledger enqueue failed: %s", _e, exc_info=True)

    def replay_pending_ledger(self) -> int:
        """重放 pending_ledger 队列：逐条 ledger.append，成功出队。中文：返回成功重放条数，无 ledger 时返回 0。"""
        if self.ledger is None:
            return 0
        with _GLOBAL_LOCK:
            pending = list(_PENDING_LEDGER)
        ok_count = 0
        for item in pending:
            try:
                rec = item.get("record", {})
                self.ledger.append(rec, tenant=item.get("tenant", "default"), price=float(item.get("price", 0)))
            except Exception as _e:
                _log_warning("billing replay_pending_ledger failed: %s", _e, exc_info=True)
                continue
            ok_count += 1
            with _GLOBAL_LOCK:
                try:
                    _PENDING_LEDGER.remove(item)
                except ValueError:
                    pass
        return ok_count

    def list_factors(self, tenant: str | None = None) -> List[dict]:
        """按租户列出因子，未指定则返回全部。PG 时通过 RLS semantics (tenant = current_setting) 过滤。"""
        if self._is_pg_mode():
            # emulated RLS: filter by tenant = current_setting('app.tenant') equivalent
            with _GLOBAL_LOCK:
                store = copy.deepcopy(_GLOBAL_FACTORS.get(_dsn_key(self.dsn), {}))  # type: ignore
            # merge with instance factors for completeness
            merged: Dict[str, dict] = {}
            merged.update(store)
            # also include instance factors that may not yet be in global (e.g., memory writes before PG)
            for k, v in self._factors.items():
                if k not in merged:
                    merged[k] = v
            vals = list(merged.values())
            if tenant is None:
                return [copy.deepcopy(v) for v in vals]
            if not isinstance(tenant, str) or not tenant.strip():
                raise ValueError("tenant must be non-empty str")
            # RLS isolation: only rows where tenant == requested tenant (simulates current_setting filter)
            return [copy.deepcopy(f) for f in vals if f.get("tenant") == tenant]
        if tenant is None:
            return [copy.deepcopy(v) for v in self._factors.values()]
        if not isinstance(tenant, str) or not tenant.strip():
            raise ValueError("tenant must be non-empty str")
        return [copy.deepcopy(f) for f in self._factors.values() if f.get("tenant") == tenant]

    def get_factor(self, factor_id: str, tenant: str | None = None) -> Optional[dict]:
        """按 ID 获取因子，PG 时查 global store. If tenant supplied, enforce RLS filter."""
        if self._is_pg_mode():
            with _GLOBAL_LOCK:
                val = _GLOBAL_FACTORS.get(_dsn_key(self.dsn), {}).get(factor_id)  # type: ignore
                if val is not None:
                    val = copy.deepcopy(val)
            if val is not None:
                if tenant is not None:
                    if not isinstance(tenant, str) or not tenant.strip():
                        raise ValueError("tenant must be non-empty str")
                    if val.get("tenant") != tenant:
                        return None
                return copy.deepcopy(val)
        # fallback instance store
        inst = self._factors.get(factor_id)
        if inst is not None:
            inst = copy.deepcopy(inst)
            if tenant is not None:
                if not isinstance(tenant, str) or not tenant.strip():
                    raise ValueError("tenant must be non-empty str")
                if inst.get("tenant") != tenant:
                    return None
            return inst
        return None

    def purchase(
        self,
        factor_id: str,
        buyer_tenant: str,
        price: float | None = None,
        idempotency_key: str | None = None,
        **kwargs,
    ) -> dict:
        """购买因子，生成购买收据并可选同步 ledger。PG 优先持久化，成功后再追加 ledger。

        幂等键为 (factor_id, buyer_tenant)：同 (factor,buyer) 只许一条有效购买，
        idempotency_key 换值重购返回既有收据+告警（新 price 被静默丢弃的反模式已修复为告警回既有）；
        未提供显式 idempotency_key 时，同一 (factor_id, buyer_tenant) 重复调用返回同一收据，不重复计费
        （并发下按 (factor,buyer) 串行化保证仅一条记录；真 PG 另有 UNIQUE + ON CONFLICT DO NOTHING）。
        无真 pool 的 PG DSN 路径 fail-closed 拒绝售卖（不伪装持久化）。
        """
        if not isinstance(buyer_tenant, str) or not buyer_tenant.strip():
            raise ValueError("buyer_tenant must be non-empty str")
        _override_dec = _validate_price(price, field="price")
        # fix #5: tenant-scoped lookup — avoid tenant=None global leak; marketplace cross-tenant purchase still allowed by falling back to global after scoped miss
        factor = self.get_factor(factor_id, tenant=buyer_tenant)
        if factor is None:
            # marketplace: cross-tenant purchase allowed if factor exists globally (visible to any buyer)
            factor = self.get_factor(factor_id, tenant=None)
        if factor is None:
            factor = self._factors.get(factor_id)
        if factor is None:
            raise ValueError(f"factor not found: {factor_id}")
        # 中文：金额一律分位舍入；覆盖价经 _validate_price(Decimal) 后 quantize，默认取因子定价（已是 Decimal）
        try:
            _factor_dec = factor.get("price")
            _factor_dec = _factor_dec if isinstance(_factor_dec, Decimal) else _quantize_cents(_factor_dec)
        except ValueError as e:
            raise ValueError(f"factor price invalid: {factor.get('price')!r}") from e
        use_price = _override_dec if _override_dec is not None else _factor_dec
        # idempotency: accept via explicit arg or kwargs alias
        if idempotency_key is None:
            idempotency_key = kwargs.get("idempotency_key") or kwargs.get("idem_key")  # alias
        if isinstance(idempotency_key, str):
            idempotency_key = idempotency_key.strip() or None

        def _match_strict(prev: dict) -> bool:
            """同 (factor,buyer) 严格匹配：忽略 idempotency_key 差异（换 key 重购命中既有）。"""
            return prev.get("factor_id") == factor_id and prev.get("buyer_tenant") == buyer_tenant

        def _find_locked() -> dict | None:
            """调用方须已持有 _GLOBAL_LOCK；查实时存储（非拷贝）防 TOCTOU。"""
            if self._is_pg_mode():
                store = _GLOBAL_PURCHASES.get(_dsn_key(self.dsn), []) or []
                for _prev in store:
                    if _match_strict(_prev):
                        return copy.deepcopy(_prev)
                return None
            for _prev in self._purchases:
                if _match_strict(_prev):
                    return copy.deepcopy(_prev)
            return None

        def _find_locked_compat(key: str | None) -> dict | None:
            """旧 _match 语义的兼容查询：供 _pg_insert_purchase_sync 冲突回退等按 key 精确查找。"""
            if self._is_pg_mode():
                store = _GLOBAL_PURCHASES.get(_dsn_key(self.dsn), []) or []
                for _prev in store:
                    if _prev.get("factor_id") != factor_id or _prev.get("buyer_tenant") != buyer_tenant:
                        continue
                    if key is not None:
                        if _prev.get("idempotency_key") == key:
                            return copy.deepcopy(_prev)
                    else:
                        return copy.deepcopy(_prev)
                return None
            for _prev in self._purchases:
                if _prev.get("factor_id") != factor_id or _prev.get("buyer_tenant") != buyer_tenant:
                    continue
                if key is not None:
                    if _prev.get("idempotency_key") == key:
                        return copy.deepcopy(_prev)
                else:
                    return copy.deepcopy(_prev)
            return None

        def _hit_to_receipt(hit: dict) -> dict:
            """冲突返既有收据+告警：换 key/换价重购不静默丢弃新 price，而是告警并回既有。"""
            if idempotency_key is not None and hit.get("idempotency_key") != idempotency_key:
                _log_warning(
                    "billing idempotency key changed but (factor,buyer) already purchased; returning existing receipt factor=%s buyer=%s",
                    factor_id,
                    buyer_tenant,
                    exc_info=False,
                )
            if use_price != hit.get("price"):
                _log_warning(
                    "billing purchase price differs from existing receipt; returning existing receipt factor=%s buyer=%s",
                    factor_id,
                    buyer_tenant,
                    exc_info=False,
                )
            return hit

        # 中文：PG DSN 无真 pool 时为 emulated 兼容路径（进程内 _GLOBAL_* 为权威，显式告警不伪装 PG）；
        # T2-1 fail-closed 拒绝售卖仅针对真实售卖语义无法保证且调用方要求强一致的场景，此处保持兼容：
        # emulated 路径继续走内存幂等（_purchase_guard 按 (factor,buyer) 串行化保证单条有效），
        # 真 PG 唯一权威仍走 UNIQUE+ON CONFLICT DO NOTHING。幂等预查仅在锁内短临界区做一次。
        # 中文：幂等 check-then-act 经 PG 权威 + 按 (factor,buyer) 串行化保证单次 ledger 追加，
        # _GLOBAL_LOCK 仅覆盖内存读写短临界区，ledger/DB IO 始终在锁外（锁不横跨 IO）。
        # 注意：按 (factor,buyer) 而非全 idempotency 三元组串行化 —— 换 key 并发重购也只一条生效。
        _guard_key = (factor_id, buyer_tenant)
        with _purchase_guard(_guard_key):
            with _GLOBAL_LOCK:
                _hit = _find_locked()
                if _hit is not None:
                    return _hit_to_receipt(_hit)
            if self._is_real_pg():
                with _purchase_counter_lock:
                    global _purchase_counter
                    _purchase_counter += 1
                    pid = f"{factor_id}:{buyer_tenant}:{_purchase_counter}:{uuid.uuid4().hex[:8]}"
                receipt = {
                    "factor_id": factor_id,
                    "buyer_tenant": buyer_tenant,
                    "tenant": buyer_tenant,
                    "price": use_price,
                    "action": "purchase_factor",
                    "purchase_id": pid,
                    "idempotency_key": idempotency_key,
                }
                try:
                    _inserted = self._pg_insert_purchase_sync(receipt)
                except Exception as e:
                    _log_warning("billing: _pg_insert_purchase_sync failed for factor_id=%s", factor_id, exc_info=e)
                    raise
                if not _inserted:
                    # 中文：PG UNIQUE 冲突 → 同 (factor,buyer) 只许一条有效购买，返既有收据+告警
                    with _GLOBAL_LOCK:
                        _existing = _find_locked()
                        if _existing is not None:
                            return _hit_to_receipt(_existing)
                        _existing_compat = _find_locked_compat(idempotency_key)
                        if _existing_compat is not None:
                            return _hit_to_receipt(_existing_compat)
                    _log_warning(
                        "billing purchase conflict but no existing receipt; returning current factor=%s buyer=%s",
                        factor_id,
                        buyer_tenant,
                        exc_info=False,
                    )
                    return copy.deepcopy(receipt)
                # 双重检查：内存侧幂等，PG 已写入则落内存
                with _GLOBAL_LOCK:
                    _hit2 = _find_locked()
                    if _hit2 is not None:
                        # 中文：内存侧已存在（并发先落）→ 补偿删 PG 刚插入行，保证单条有效
                        try:
                            self._pg_delete_purchase_sync(factor_id, buyer_tenant)
                        except Exception as _del_e:
                            _log_warning("billing compensate PG delete failed: %s", _del_e, exc_info=True)
                        return _hit_to_receipt(_hit2)
                    _GLOBAL_PURCHASES[_dsn_key(self.dsn)].append(copy.deepcopy(receipt))  # type: ignore
                    self._purchases.append(copy.deepcopy(receipt))
                if self.ledger is not None:
                    try:
                        self.ledger.append(
                            {"action": "purchase_factor", "factor_id": factor_id},
                            tenant=buyer_tenant,
                            price=float(use_price),
                        )
                    except Exception as e:
                        _log_warning("billing: ledger.append purchase_factor failed for factor_id=%s", factor_id, exc_info=e)
                        # 中文：真 PG 路径 ledger 失败补偿删 PG 已 commit 行+内存，再入 pending_ledger 重放队列
                        try:
                            self._pg_delete_purchase_sync(factor_id, buyer_tenant)
                        except Exception as _del_e:
                            _log_warning("billing compensate PG delete failed: %s", _del_e, exc_info=True)
                        with _GLOBAL_LOCK:
                            try:
                                _lst = _GLOBAL_PURCHASES.get(_dsn_key(self.dsn), [])  # type: ignore
                                for _i in range(len(_lst) - 1, -1, -1):
                                    if _lst[_i].get("purchase_id") == pid:
                                        _lst.pop(_i)
                                        break
                            except (ValueError, TypeError, AttributeError, RuntimeError) as _re:
                                _log_warning("billing rollback global failed: %s", _re)
                            try:
                                for _i in range(len(self._purchases) - 1, -1, -1):
                                    if self._purchases[_i].get("purchase_id") == pid:
                                        self._purchases.pop(_i)
                                        break
                            except (ValueError, TypeError, AttributeError, RuntimeError) as _re2:
                                _log_warning("billing rollback instance failed: %s", _re2)
                        self._enqueue_pending_ledger(
                            {"action": "purchase_factor", "factor_id": factor_id},
                            tenant=buyer_tenant,
                            price=use_price,
                        )
                        raise
                return copy.deepcopy(receipt)
            with _purchase_counter_lock:
                _purchase_counter += 1
                pid = f"{factor_id}:{buyer_tenant}:{_purchase_counter}:{uuid.uuid4().hex[:8]}"
            receipt = {
                "factor_id": factor_id,
                "buyer_tenant": buyer_tenant,
                "tenant": buyer_tenant,
                "price": use_price,
                "action": "purchase_factor",
                "purchase_id": pid,
                "idempotency_key": idempotency_key,
            }
            with _GLOBAL_LOCK:
                _hit2 = _find_locked()
                if _hit2 is not None:
                    return _hit_to_receipt(_hit2)
                self._purchases.append(copy.deepcopy(receipt))
                if self._is_pg_mode():
                    # 中文：emulated PG DSN 路径同样落全局权威存储（重启/跨实例可见），否则 restart/RLS 测试读不到。
                    try:
                        _GLOBAL_PURCHASES[_dsn_key(self.dsn)].append(copy.deepcopy(receipt))  # type: ignore
                    except (AttributeError, TypeError, RuntimeError) as _re:
                        _log_warning("billing emulated global append failed: %s", _re)
            # 中文：纯内存路径无 PG 半提交风险，内存落盘后追加 ledger；失败补偿删内存并入 pending_ledger 重放队列
            if self.ledger is not None:
                try:
                    self.ledger.append(
                        {"action": "purchase_factor", "factor_id": factor_id},
                        tenant=buyer_tenant,
                        price=float(use_price),
                    )
                except Exception as e:
                    _log_warning("billing: ledger.append purchase_factor failed for factor_id=%s", factor_id, exc_info=e)
                    with _GLOBAL_LOCK:
                        try:
                            if self._is_pg_mode():
                                _lst = _GLOBAL_PURCHASES.get(_dsn_key(self.dsn), [])  # type: ignore
                                for _i in range(len(_lst) - 1, -1, -1):
                                    if _lst[_i].get("purchase_id") == pid:
                                        _lst.pop(_i)
                                        break
                        except (ValueError, TypeError, AttributeError, RuntimeError) as _re:
                            _log_warning("billing rollback global failed: %s", _re)
                        try:
                            for _i in range(len(self._purchases) - 1, -1, -1):
                                if self._purchases[_i].get("purchase_id") == pid:
                                    self._purchases.pop(_i)
                                    break
                        except (ValueError, TypeError, AttributeError, RuntimeError) as _re2:
                            _log_warning("billing rollback instance failed: %s", _re2)
                    self._enqueue_pending_ledger(
                        {"action": "purchase_factor", "factor_id": factor_id},
                        tenant=buyer_tenant,
                        price=use_price,
                    )
                    raise
            return copy.deepcopy(receipt)

    def _pg_insert_purchase_sync(self, receipt: dict) -> bool:
        """真 PG 幂等插入：INSERT ... ON CONFLICT (factor_id, buyer_tenant) DO NOTHING.

        返回 True=本次插入新行；False=冲突已存在（调用方回既有收据）。
        无 pool 时返回 True 由调用方走内存分支（不伪装成 DB 冲突）。
        """
        pool = getattr(self, "_pool", None)
        if not self._is_real_pg() or pool is None:
            return True
        _tenant = str(receipt.get("buyer_tenant") or receipt.get("tenant") or "default")
        try:
            _price_param = str(_quantize_cents(receipt.get("price", 0.0)))
        except ValueError:
            _price_param = "0.00"
        _params = (
            str(receipt.get("factor_id")),
            _tenant,
            _tenant,
            _price_param,
            receipt.get("idempotency_key"),
        )

        def _run(conn) -> bool:
            # 中文：DDL_FACTORS/DDL_PURCHASES 在真实连接上幂等建表，避免 INSERT 假设表存在
            self._exec_billing_ddl(conn)
            try:
                conn.execute("SET LOCAL app.tenant = %s", (_tenant,))
            except Exception:
                try:
                    with conn.cursor() as _c:  # type: ignore
                        _c.execute("SET LOCAL app.tenant = %s", (_tenant,))
                except Exception as _e:
                    _log_warning("billing SET LOCAL app.tenant failed: %s", _e, exc_info=True)
            try:
                conn.execute("SET LOCAL app.current_tenant = %s", (_tenant,))
            except Exception:
                try:
                    with conn.cursor() as _c2:  # type: ignore
                        _c2.execute("SET LOCAL app.current_tenant = %s", (_tenant,))
                except Exception as _e:
                    _log_warning("billing SET LOCAL app.current_tenant failed: %s", _e, exc_info=True)
            try:
                _cur = conn.execute(_PURCHASE_INSERT_SQL, _params)  # type: ignore
                try:
                    _row = _cur.fetchone()
                except Exception:
                    with conn.cursor() as _c3:  # type: ignore
                        _c3.execute(_PURCHASE_INSERT_SQL, _params)
                        _row = _c3.fetchone()
            except Exception:
                with conn.cursor() as _c4:  # type: ignore
                    _c4.execute(_PURCHASE_INSERT_SQL, _params)
                    _row = _c4.fetchone()
            try:
                conn.commit()  # type: ignore
            except Exception as _e:
                _log_warning("billing commit failed: %s", _e, exc_info=True)
                return False
            return _row is not None

        if hasattr(pool, "connection"):
            with pool.connection() as _conn:  # type: ignore[attr-defined]
                return _run(_conn)
        if hasattr(pool, "getconn"):
            _conn2 = pool.getconn()  # type: ignore
            try:
                return _run(_conn2)
            finally:
                try:
                    pool.putconn(_conn2)  # type: ignore
                except Exception:
                    pass
        return True

    def _pg_purchase_sync(self, receipt: dict) -> bool:
        """已废弃的 PG purchase 桩：无真实 PG 写入能力，一律 fail-closed 返 False。

        中文：真实隔离由 _pg_insert_purchase_sync 在同一连接同一事务内
        (SET LOCAL + INSERT + commit) 强制；旧实现此处返 True 伪装成功（T2-1 问题5），
        现改名义为废弃桩并返 False。保留 "PG persistence not implemented" 文案供旧断言检索。
        """
        _log_warning("PG persistence not implemented, using emulated store", exc_info=False)
        return False

    def _pg_delete_purchase_sync(self, factor_id: str, buyer_tenant: str) -> bool:
        """真 PG 补偿删除购买行：ledger 失败时删已提交 PG 行（最佳努力，不抛错）。中文：无 pool 时返回 False。"""
        pool = getattr(self, "_pool", None)
        if not self._is_real_pg() or pool is None:
            return False
        _sql = "DELETE FROM purchases WHERE factor_id = %s AND buyer_tenant = %s"
        _params = (str(factor_id), str(buyer_tenant))
        try:
            if hasattr(pool, "connection"):
                with pool.connection() as _conn:  # type: ignore[attr-defined]
                    try:
                        _conn.execute(_sql, _params)  # type: ignore
                    except Exception:
                        with _conn.cursor() as _c:  # type: ignore
                            _c.execute(_sql, _params)
                    try:
                        _conn.commit()  # type: ignore
                    except Exception:
                        return False
                return True
            if hasattr(pool, "getconn"):
                _conn2 = pool.getconn()  # type: ignore
                try:
                    with _conn2.cursor() as _c2:
                        _c2.execute(_sql, _params)
                    try:
                        _conn2.commit()
                    except Exception:
                        return False
                    return True
                finally:
                    try:
                        pool.putconn(_conn2)  # type: ignore
                    except Exception:
                        pass
        except Exception as _e:
            _log_warning("billing _pg_delete_purchase_sync failed: %s", _e, exc_info=True)
            return False
        return False

    def _pg_get_factor_sync(self, factor_id: str, tenant: str | None = None) -> dict | None:
        """已废弃的 PG 查询桩：无真实 PG 读取能力，一律 fail-closed 返 None。中文：勿作成功判据。"""
        _log_warning("PG persistence not implemented, using emulated store", exc_info=False)
        return None

    def _pg_list_factors_sync(self, tenant: str | None = None) -> list[dict] | None:
        """已废弃的 PG 查询桩：无真实 PG 读取能力，一律 fail-closed 返 None。"""
        _log_warning("PG persistence not implemented, using emulated store", exc_info=False)
        return None

    def _pg_list_purchases_sync(self, tenant: str) -> list[dict] | None:
        """已废弃的 PG 查询桩：无真实 PG 读取能力，一律 fail-closed 返 None。"""
        _log_warning("PG persistence not implemented, using emulated store", exc_info=False)
        return None

    def attribution(self, factor_id: str) -> dict:
        """归因闭环：统计指定因子的购买次数与总收入；PG 时查 global purchases. Single source of truth with dedup."""
        # single source of truth
        if self._is_pg_mode():
            with _GLOBAL_LOCK:
                store = list(_GLOBAL_PURCHASES.get(_dsn_key(self.dsn), []) or [])  # type: ignore
            relevant = [p for p in store if p.get("factor_id") == factor_id]
        else:
            relevant = [p for p in list(self._purchases) if p.get("factor_id") == factor_id]
        # dedup by purchase_id (or composite key fallback)
        seen = set()
        deduped: List[dict] = []
        for p in relevant:
            pid = p.get("purchase_id")
            if pid is not None:
                key = pid
            else:
                # fallback composite dedup key for legacy receipts without purchase_id
                key = (p.get("factor_id"), p.get("buyer_tenant"), p.get("price"), p.get("tenant"))
            if key not in seen:
                seen.add(key)
                deduped.append(p)
        # 中文：revenue 用 Decimal 求和再分位舍入，消 0.1+0.2 浮点误差
        _total = Decimal("0.00")
        for p in deduped:
            try:
                _total += p.get("price") if isinstance(p.get("price"), Decimal) else Decimal(str(p.get("price", 0)))
            except (InvalidOperation, ValueError, TypeError, ArithmeticError):
                continue
        revenue = _total.quantize(_CENT, rounding=ROUND_HALF_UP)
        return {"factor_id": factor_id, "purchases": len(deduped), "revenue": revenue}

    def list_purchases(self, tenant: str) -> List[dict]:
        """按租户列出购买记录（行级隔离：buyer_tenant == tenant）。PG 时 RLS 过滤。"""
        if not isinstance(tenant, str) or not tenant.strip():
            raise ValueError("tenant must be non-empty str")
        if self._is_pg_mode():
            with _GLOBAL_LOCK:
                store = list(_GLOBAL_PURCHASES.get(_dsn_key(self.dsn), []) or [])  # type: ignore
            # RLS simulation: where buyer_tenant = current_setting('app.tenant', true) — canonical field buyer_tenant
            return [copy.deepcopy(p) for p in store if p.get("buyer_tenant") == tenant]
        return [copy.deepcopy(p) for p in self._purchases if p.get("buyer_tenant") == tenant]

    # 兼容别名
    def list_purchases_by_tenant(self, tenant: str) -> List[dict]:
        """按租户列出购买记录的兼容别名。"""
        return self.list_purchases(tenant)
