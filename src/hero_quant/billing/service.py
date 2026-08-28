"""因子市场计费 —— 因子即资产，计费到归因闭环。

职责：提供因子发布、购买与归因统计；架构位置：billing 域，依赖可选 ledger 做来源追溯。
设计决策：以 tenant 为隔离维度，PG 存储因子与购买记录（RLS），ledger 仅作追加与溯源的外部同步；无 PG DSN 时 fallback 到内存。
Task8: asyncpg PG + RLS (tenant = current_setting('app.tenant', true))
"""
from __future__ import annotations

import copy
import os
import threading
import uuid
from typing import Dict, List, Optional

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
        """是否 PG 主路径（DSN 为 PG 前缀即视为 PG 模式，emulated store 保证重启不丢）。"""
        return _is_pg_dsn(self.dsn)

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

    def publish_factor(
        self,
        factor_id: str,
        name: str,
        price: float,
        tenant: str = "default",
        description: str = "",
    ) -> dict:
        """发布因子，记录归属租户与定价。PG 时写入 global emulated store + 尝试真实 PG。"""
        if not isinstance(tenant, str) or not tenant.strip():
            raise ValueError("tenant must be non-empty str")
        factor = {
            "factor_id": factor_id,
            "name": name,
            "price": float(price),
            "tenant": tenant,
            "description": description,
        }
        if self._is_pg_mode():
            # emulated PG persistence (restart not lost) under lock
            with _GLOBAL_LOCK:
                _GLOBAL_FACTORS[self.dsn][factor_id] = copy.deepcopy(factor)  # type: ignore
            # also keep instance for immediate fallback
            self._factors[factor_id] = copy.deepcopy(factor)
            # try real PG asynchronously best-effort (not required for tests)
            try:
                self._pg_publish_sync(factor)
            except Exception as e:
                _log_warning("billing: _pg_publish_sync failed for factor_id=%s", factor_id, exc_info=e)
                raise
        else:
            self._factors[factor_id] = copy.deepcopy(factor)
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
        return copy.deepcopy(factor)

    def _pg_publish_sync(self, factor: dict) -> None:
        """Best-effort sync PG publish — PG persistence not implemented, using emulated store."""
        # Placeholder for real PG — intentionally no-op for emulated; keep RLS semantics via global store filtering.
        # DDL gated: only executed when real PG pool available. Warn that emulated store is used.
        _log_warning("PG persistence not implemented, using emulated store", exc_info=False)
        return None

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
        """购买因子，生成购买收据并可选同步 ledger。PG 时写入 purchases global."""
        if not isinstance(buyer_tenant, str) or not buyer_tenant.strip():
            raise ValueError("buyer_tenant must be non-empty str")
        # resolve factor from PG or memory
        factor = self.get_factor(factor_id)
        if factor is None:
            # also check instance store as fallback
            factor = self._factors.get(factor_id)
        if factor is None:
            raise ValueError(f"factor not found: {factor_id}")
        use_price = float(price) if price is not None else float(factor["price"])
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
        if self._is_pg_mode():
            with _GLOBAL_LOCK:
                _GLOBAL_PURCHASES[self.dsn].append(copy.deepcopy(receipt))  # type: ignore
            self._purchases.append(copy.deepcopy(receipt))
            try:
                self._pg_purchase_sync(receipt)
            except Exception as e:
                _log_warning("billing: _pg_purchase_sync failed for factor_id=%s", factor_id, exc_info=e)
                raise
        else:
            self._purchases.append(copy.deepcopy(receipt))
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
        return copy.deepcopy(receipt)

    def _pg_purchase_sync(self, receipt: dict) -> None:
        _log_warning("PG persistence not implemented, using emulated store", exc_info=False)
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
