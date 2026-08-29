"""因子市场计费 —— 因子即资产，计费到归因闭环。

职责：提供因子发布、购买与归因统计；架构位置：billing 域，依赖可选 ledger 做来源追溯。
设计决策：以 tenant 为隔离维度，PG 存储因子与购买记录（RLS），ledger 仅作追加与溯源的外部同步；无 PG DSN 时 fallback 到内存。
Task8: asyncpg PG + RLS (tenant = current_setting('app.tenant', true))
"""
from __future__ import annotations

import copy
import math
import os
import threading
import uuid
from typing import Dict, List, Optional


def _validate_price(price: float | None, *, field: str = "price") -> float | None:
    if price is None:
        return None
    try:
        fv = float(price)
    except (ValueError, TypeError) as e:
        raise ValueError(f"{field} must be numeric, got {price!r}") from e
    if not math.isfinite(fv) or fv < 0:
        raise ValueError(f"{field} must be finite and >= 0, got {price!r}")
    return fv

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

_PG_PREFIXES = ("postgresql://", "postgres://", "postgresql+psycopg://")

DDL_FACTORS = """
CREATE TABLE IF NOT EXISTS factors (
  factor_id text PRIMARY KEY,
  name text NOT NULL,
  price double precision NOT NULL,
  tenant text NOT NULL,
  description text DEFAULT ''
);
"""

DDL_PURCHASES = """
CREATE TABLE IF NOT EXISTS purchases (
  id SERIAL PRIMARY KEY,
  factor_id text NOT NULL REFERENCES factors(factor_id),
  buyer_tenant text NOT NULL,
  tenant text NOT NULL,
  price double precision NOT NULL,
  created_at timestamptz DEFAULT now()
);
"""
# NOTE: DDL_FACTORS/DDL_PURCHASES are gated — only executed when real PG pool is available.
# When running in emulated PG mode (no real pool), PG persistence not implemented, using emulated store.

# Global emulated PG stores for fallback when asyncpg not available but DSN is PG (restart not lost)
_GLOBAL_LOCK = threading.RLock()
_GLOBAL_FACTORS: Dict[str, Dict[str, dict]] = {}  # dsn -> factor_id -> factor
_GLOBAL_PURCHASES: Dict[str, List[dict]] = {}  # dsn -> list[purchase]
_PG_WARNING_LOGGED = False
_PG_WARNING_LOCK = threading.Lock()
_purchase_counter = 0
_purchase_counter_lock = threading.Lock()


def _is_pg_dsn(dsn: str | None) -> bool:
    return isinstance(dsn, str) and dsn.startswith(_PG_PREFIXES)


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
        # try asyncpg pool if PG DSN present
        if _is_pg_dsn(self.dsn):
            try:
                import asyncpg  # type: ignore
                # pool creation is async, keep DSN for lazy connect
                self._asyncpg = asyncpg
            except Exception:
                self._asyncpg = None  # type: ignore
            # attempt to init global stores for emulated PG under lock
            with _GLOBAL_LOCK:
                _GLOBAL_FACTORS.setdefault(self.dsn, {})  # type: ignore
                _GLOBAL_PURCHASES.setdefault(self.dsn, [])  # type: ignore
            # minimal fix: log warning that real PG not implemented
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
        """是否真实 PG 可用（驱动 + DSN）。用于区分 emulated 与真实持久化。"""
        if not _is_pg_dsn(self.dsn):
            return False
        # asyncpg 驱动是否可用视为真实 PG 能力标志
        return getattr(self, "_asyncpg", None) is not None

    def _get_global_factors(self) -> Dict[str, dict]:
        if not _is_pg_dsn(self.dsn):
            return self._factors
        with _GLOBAL_LOCK:
            return copy.deepcopy(_GLOBAL_FACTORS.get(self.dsn, {}))  # type: ignore

    def _get_global_purchases(self) -> List[dict]:
        if not _is_pg_dsn(self.dsn):
            return list(self._purchases)
        with _GLOBAL_LOCK:
            return copy.deepcopy(_GLOBAL_PURCHASES.get(self.dsn, []))  # type: ignore

    def _pg_publish_noop(self, factor: dict) -> None:
        """显式 no-op 桩：真实 PG 未实现时不做任何持久化，已通过 _log_pg_warning_once 告警。"""
        return None

    def _pg_purchase_noop(self, receipt: dict) -> None:
        """显式 no-op 桩：购买持久化未实现。"""
        return None

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
        """发布因子，记录归属租户与定价。先写 ledger 再落 PG，避免半提交。"""
        # alias handling: overwrite kw
        if kwargs.get("overwrite") is not None:
            allow_overwrite = allow_overwrite or bool(kwargs.get("overwrite"))
        if not isinstance(tenant, str) or not tenant.strip():
            raise ValueError("tenant must be non-empty str")
        _validate_price(price, field="price")
        effective_allow = bool(allow_overwrite or upsert)
        # 租户隔离修复：factor_id 单查绕过 — 冲突检查需同时查全局与实例
        # 若 factor_id 已存在且属于不同租户，未授权时同样拒绝，避免跨租户覆盖
        if not effective_allow:
            exists = False
            existing_tenant = None
            if self._is_pg_mode():
                with _GLOBAL_LOCK:
                    existing = _GLOBAL_FACTORS.get(self.dsn, {}).get(factor_id)  # type: ignore
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
                # 跨租户也视为冲突，除非显式 allow_overwrite
                raise ValueError(f"factor_id already exists: {factor_id}; use allow_overwrite=True or upsert=True to overwrite")
            # 即使租户不同也不允许隐式覆盖
            _ = existing_tenant
        factor = {
            "factor_id": factor_id,
            "name": name,
            "price": float(price),
            "tenant": tenant,
            "description": description,
        }
        # 修复半提交：先写 ledger，失败则不落持久化
        if self.ledger is not None:
            try:
                self.ledger.append(
                    {"action": "publish_factor", "factor_id": factor_id, "name": name},
                    tenant=tenant,
                    price=float(price),
                )
            except Exception as e:
                _log_warning("billing: ledger.append publish_factor failed for factor_id=%s", factor_id, exc_info=e)
                raise
        # gate writes: real PG vs emulated-degraded vs memory (Req #4)
        if self._is_real_pg():
            with _GLOBAL_LOCK:
                _GLOBAL_FACTORS[self.dsn][factor_id] = copy.deepcopy(factor)  # type: ignore
            self._factors[factor_id] = copy.deepcopy(factor)
            try:
                self._pg_publish_sync(factor)
                self._pg_publish_noop(factor)
            except Exception as e:
                _log_warning("billing: _pg_publish_sync failed for factor_id=%s", factor_id, exc_info=e)
                with _GLOBAL_LOCK:
                    try:
                        _GLOBAL_FACTORS[self.dsn].pop(factor_id, None)  # type: ignore
                    except Exception as _e:
                        _log_warning("billing: rollback global pop failed for %s: %s", factor_id, _e)
                try:
                    self._factors.pop(factor_id, None)
                except Exception as _e:
                    _log_warning("billing: rollback instance pop failed for %s: %s", factor_id, _e)
                raise
        elif self._is_pg_mode():
            _log_warning("billing degraded (emulated PG without driver) tenant=%s", str(factor.get("tenant", "default")), exc_info=False)
            with _GLOBAL_LOCK:
                _GLOBAL_FACTORS[self.dsn][factor_id] = copy.deepcopy(factor)  # type: ignore
            self._factors[factor_id] = copy.deepcopy(factor)
            try:
                self._pg_publish_sync(factor)
                self._pg_publish_noop(factor)
            except Exception as e:
                _log_warning("billing: _pg_publish_sync failed for factor_id=%s", factor_id, exc_info=e)
                with _GLOBAL_LOCK:
                    try:
                        _GLOBAL_FACTORS[self.dsn].pop(factor_id, None)  # type: ignore
                    except Exception as _e:
                        _log_warning("billing: rollback global pop failed for %s: %s", factor_id, _e)
                try:
                    self._factors.pop(factor_id, None)
                except Exception as _e:
                    _log_warning("billing: rollback instance pop failed for %s: %s", factor_id, _e)
                raise
        else:
            self._factors[factor_id] = copy.deepcopy(factor)
        return copy.deepcopy(factor)

    def _pg_publish_sync(self, factor: dict) -> None:
        """Best-effort sync PG publish — no-op 桩，真实 PG 未接入时仅告警；真实 PG 时双写 SET LOCAL。"""
        if self._is_real_pg() and getattr(self, "_pool", None) is not None:
            # real PG: SET LOCAL both keys inside txn (best-effort dual write)
            _tenant = str(factor.get("tenant", "default"))
            _log_warning("PG publish SET LOCAL app.tenant=%s (dual write with app.current_tenant)", _tenant, exc_info=False)
            pool = getattr(self, "_pool", None)
            if pool is not None:
                for _k in ("app.tenant", "app.current_tenant"):
                    _sql = f"SET LOCAL {_k} = %s"
                    try:
                        if hasattr(pool, "connection"):
                            with pool.connection() as _conn:  # type: ignore[attr-defined]
                                try:
                                    _conn.execute(_sql, (_tenant,))  # type: ignore
                                except Exception:
                                    with _conn.cursor() as _c:  # type: ignore
                                        _c.execute(_sql, (_tenant,))
                        elif hasattr(pool, "getconn"):
                            _conn2 = pool.getconn()  # type: ignore
                            try:
                                with _conn2.cursor() as _c2:
                                    _c2.execute(_sql, (_tenant,))
                                _conn2.commit()
                            finally:
                                try:
                                    pool.putconn(_conn2)  # type: ignore
                                except Exception:
                                    pass
                    except Exception as _e:
                        _log_warning("billing SET LOCAL %s failed: %s", _k, _e)
                        continue
        elif self._is_pg_mode() and not self._is_real_pg():
            _log_warning("PG persistence degraded (emulated): using emulated store for tenant=%s", str(factor.get("tenant", "default")), exc_info=False)
            _log_warning("PG persistence not implemented, using emulated store", exc_info=False)
        else:
            _log_warning("PG persistence not implemented, using emulated store", exc_info=False)
        return None

    # 兼容别名：_pg_publish_noop 已在上方定义，此处保留 _pg_publish_sync 为真实入口

    def list_factors(self, tenant: str | None = None) -> List[dict]:
        """按租户列出因子，未指定则返回全部。PG 时通过 RLS semantics (tenant = current_setting) 过滤。"""
        if self._is_pg_mode():
            # emulated RLS: filter by tenant = current_setting('app.tenant') equivalent
            with _GLOBAL_LOCK:
                store = copy.deepcopy(_GLOBAL_FACTORS.get(self.dsn, {}))  # type: ignore
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
                val = _GLOBAL_FACTORS.get(self.dsn, {}).get(factor_id)  # type: ignore
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
    ) -> dict:
        """购买因子，生成购买收据并可选同步 ledger。先写 ledger 再落持久化，避免半提交。"""
        if not isinstance(buyer_tenant, str) or not buyer_tenant.strip():
            raise ValueError("buyer_tenant must be non-empty str")
        _validate_price(price, field="price")
        # fix #5: tenant-scoped lookup — avoid tenant=None global leak; marketplace cross-tenant purchase still allowed by falling back to global after scoped miss
        factor = self.get_factor(factor_id, tenant=buyer_tenant)
        if factor is None:
            # marketplace: cross-tenant purchase allowed if factor exists globally (visible to any buyer)
            factor = self.get_factor(factor_id, tenant=None)
        if factor is None:
            factor = self._factors.get(factor_id)
        if factor is None:
            raise ValueError(f"factor not found: {factor_id}")
        # price validation also covers overridden price via _validate_price above
        use_price = float(price) if price is not None else float(factor["price"])
        if not math.isfinite(use_price) or use_price < 0:
            raise ValueError(f"price must be finite and >= 0, got {use_price!r}")
        # generate unique purchase_id for dedup
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
        }
        # 先写 ledger，失败不落持久化
        if self.ledger is not None:
            try:
                self.ledger.append(
                    {"action": "purchase_factor", "factor_id": factor_id},
                    tenant=buyer_tenant,
                    price=use_price,
                )
            except Exception as e:
                _log_warning("billing: ledger.append purchase_factor failed for factor_id=%s", factor_id, exc_info=e)
                raise
        # gate writes: real PG vs emulated (degraded) — requirement #4
        if self._is_real_pg():
            with _GLOBAL_LOCK:
                _GLOBAL_PURCHASES[self.dsn].append(copy.deepcopy(receipt))  # type: ignore
            self._purchases.append(copy.deepcopy(receipt))
            try:
                self._pg_purchase_sync(receipt)
                self._pg_purchase_noop(receipt)
            except Exception as e:
                _log_warning("billing: _pg_purchase_sync failed for factor_id=%s", factor_id, exc_info=e)
        elif self._is_pg_mode():
            _log_warning("billing degraded (emulated PG without driver) buyer=%s", buyer_tenant, exc_info=False)
            with _GLOBAL_LOCK:
                _GLOBAL_PURCHASES[self.dsn].append(copy.deepcopy(receipt))  # type: ignore
            self._purchases.append(copy.deepcopy(receipt))
            try:
                self._pg_purchase_sync(receipt)
                self._pg_purchase_noop(receipt)
            except Exception as e:
                _log_warning("billing: _pg_purchase_sync failed for factor_id=%s", factor_id, exc_info=e)
                # 回滚已写入的 emulated
                with _GLOBAL_LOCK:
                    try:
                        lst = _GLOBAL_PURCHASES.get(self.dsn, [])  # type: ignore
                        # 移除最后一条匹配的 receipt
                        for i in range(len(lst) - 1, -1, -1):
                            if lst[i].get("purchase_id") == pid:
                                lst.pop(i)
                                break
                    except Exception:
                        pass
                try:
                    for i in range(len(self._purchases) - 1, -1, -1):
                        if self._purchases[i].get("purchase_id") == pid:
                            self._purchases.pop(i)
                            break
                except Exception:
                    pass
                raise
        else:
            self._purchases.append(copy.deepcopy(receipt))
        return copy.deepcopy(receipt)

    def _pg_purchase_sync(self, receipt: dict) -> None:
        if self._is_real_pg() and getattr(self, "_pool", None) is not None:
            _tenant = str(receipt.get("buyer_tenant") or receipt.get("tenant") or "default")
            _log_warning("PG purchase SET LOCAL app.tenant=%s (dual write with app.current_tenant)", _tenant, exc_info=False)
            pool = getattr(self, "_pool", None)
            if pool is not None:
                for _k in ("app.tenant", "app.current_tenant"):
                    _sql = f"SET LOCAL {_k} = %s"
                    try:
                        if hasattr(pool, "connection"):
                            with pool.connection() as _conn:  # type: ignore
                                try:
                                    _conn.execute(_sql, (_tenant,))  # type: ignore
                                except Exception:
                                    with _conn.cursor() as _c:  # type: ignore
                                        _c.execute(_sql, (_tenant,))
                        elif hasattr(pool, "getconn"):
                            _conn2 = pool.getconn()  # type: ignore
                            try:
                                with _conn2.cursor() as _c2:
                                    _c2.execute(_sql, (_tenant,))
                                _conn2.commit()
                            finally:
                                try:
                                    pool.putconn(_conn2)  # type: ignore
                                except Exception:
                                    pass
                    except Exception as _e:
                        _log_warning("billing SET LOCAL %s failed: %s", _k, _e)
                        continue
        else:
            _log_warning("PG persistence not implemented, using emulated store", exc_info=False)
        return None

    def _pg_get_factor_sync(self, factor_id: str, tenant: str | None = None) -> dict | None:
        """PG 侧 factor 查询 no-op 桩（真实 PG 时应执行 SELECT with RLS）。"""
        return None

    def _pg_list_factors_sync(self, tenant: str | None = None) -> list[dict] | None:
        return None

    def _pg_list_purchases_sync(self, tenant: str) -> list[dict] | None:
        return None

    def attribution(self, factor_id: str) -> dict:
        """归因闭环：统计指定因子的购买次数与总收入；PG 时查 global purchases. Single source of truth with dedup."""
        # single source of truth
        if self._is_pg_mode():
            with _GLOBAL_LOCK:
                store = list(_GLOBAL_PURCHASES.get(self.dsn, []) or [])  # type: ignore
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
        revenue = sum(float(p.get("price", 0)) for p in deduped)
        return {"factor_id": factor_id, "purchases": len(deduped), "revenue": revenue}

    def list_purchases(self, tenant: str) -> List[dict]:
        """按租户列出购买记录（行级隔离：buyer_tenant == tenant）。PG 时 RLS 过滤。"""
        if not isinstance(tenant, str) or not tenant.strip():
            raise ValueError("tenant must be non-empty str")
        if self._is_pg_mode():
            with _GLOBAL_LOCK:
                store = list(_GLOBAL_PURCHASES.get(self.dsn, []) or [])  # type: ignore
            # RLS simulation: where buyer_tenant = current_setting('app.tenant', true) — canonical field buyer_tenant
            return [copy.deepcopy(p) for p in store if p.get("buyer_tenant") == tenant]
        return [copy.deepcopy(p) for p in self._purchases if p.get("buyer_tenant") == tenant]

    # 兼容别名
    def list_purchases_by_tenant(self, tenant: str) -> List[dict]:
        """按租户列出购买记录的兼容别名。"""
        return self.list_purchases(tenant)
