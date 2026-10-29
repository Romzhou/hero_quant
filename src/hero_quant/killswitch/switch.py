"""killswitch — 租户 / 模型 / 工具一键停用（W8 PR-173）。

语义：
- 作用域 `tenant`：停用整个租户；`model` / `tool`：停用某模型或某工具。
- 全局开关（tenant_id=None）对所有租户生效；带 tenant_id 时只覆盖该租户（优先级更高）。
- 开启后 `check()` 抛 `KillSwitchActive`（503 + 明确 reason），调用方直接转响应体。
- Redis 不可用 → `KillSwitchUnavailable`（503）：宁可拒绝也绝不无保护放行（与限流一致）。
- enable/disable 写审计（tenant/global），审计失败向上抛，不静默丢事件。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from hero_quant.infra.redis import get_redis

KEY_PREFIX = "hero:killswitch:"
SCOPES = frozenset({"tenant", "model", "tool"})
_GLOBAL_TENANT = "-"
_GLOBAL_AUDIT_TENANT = "__global__"

_MAX_TARGET_LEN = 128
_MAX_REASON_LEN = 512
_MAX_ACTOR_LEN = 128


class KillSwitchError(RuntimeError):
    """kill switch 错误基类。"""

    error = "killswitch_error"
    status_code = 500

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        message = f"{self.error}: {reason}"
        if detail:
            message += f" ({detail})"
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.error, "reason": self.reason}


class KillSwitchActive(KillSwitchError):
    """目标能力已被停用 —— 503。"""

    error = "killed"
    status_code = 503

    def __init__(self, scope: str, target: str, reason: str, tenant_id: str | None = None) -> None:
        self.scope = scope
        self.target = target
        self.kill_reason = reason
        self.tenant_id = tenant_id
        super().__init__(reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.error,
            "scope": self.scope,
            "target": self.target,
            "tenant_id": self.tenant_id,
            "reason": self.kill_reason,
        }


class KillSwitchUnavailable(KillSwitchError):
    """Redis 不可用 / 读写失败 —— 503（fail-closed）。"""

    error = "killswitch_unavailable"
    status_code = 503


class KillSwitchValidationError(KillSwitchError):
    """入参非法 —— 400。"""

    error = "killswitch_invalid"
    status_code = 400


def _token(value: Any, field: str, max_len: int) -> str:
    """开关键片段：不接受 `:`（键分隔符）与空白（运维脚本不可解析）。"""
    if not isinstance(value, str):
        raise KillSwitchValidationError(f"{field}_must_be_str")
    text = value.strip()
    if not text:
        raise KillSwitchValidationError(f"{field}_required")
    if len(text) > max_len:
        raise KillSwitchValidationError(f"{field}_too_long")
    if ":" in text or any(ch.isspace() for ch in text):
        raise KillSwitchValidationError(f"{field}_invalid_chars")
    return text


def _line(value: Any, field: str, max_len: int) -> str:
    """单行人类可读字段（reason/actor）：允许空格，禁止换行防日志注入。"""
    if not isinstance(value, str):
        raise KillSwitchValidationError(f"{field}_must_be_str")
    text = value.strip()
    if not text:
        raise KillSwitchValidationError(f"{field}_required")
    if len(text) > max_len:
        raise KillSwitchValidationError(f"{field}_too_long")
    if "\n" in text or "\r" in text:
        raise KillSwitchValidationError(f"{field}_must_be_single_line")
    return text


class KillSwitch:
    """Redis 标志位开关；可注入 client 与审计存储（测试友好）。"""

    def __init__(self, redis: Any = None, *, audit: Any = None, key_prefix: str = KEY_PREFIX) -> None:
        self._redis = redis
        self._audit = audit
        self._key_prefix = key_prefix

    async def _client(self) -> Any:
        client = self._redis if self._redis is not None else await get_redis()
        if client is None:
            raise KillSwitchUnavailable("redis_unavailable")
        return client

    def _key(self, scope: str, target: str, tenant_id: str | None) -> str:
        if scope not in SCOPES:
            raise KillSwitchValidationError(f"scope_must_be_one_of:{','.join(sorted(SCOPES))}")
        name = _token(target, "target", _MAX_TARGET_LEN)
        # tenant 作用域本身就是「按租户停用」，不再叠加租户前缀。
        tenant_scope = None if scope == "tenant" else tenant_id
        if tenant_scope is not None:
            tenant_scope = _token(tenant_scope, "tenant_id", _MAX_TARGET_LEN)
        return f"{self._key_prefix}{scope}:{tenant_scope or _GLOBAL_TENANT}:{name}"

    async def enable(
        self,
        scope: str,
        target: str,
        reason: str,
        *,
        actor: str,
        tenant_id: str | None = None,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """开启开关（默认永久，需显式 disable；ttl_seconds 用于临时停用）。"""
        key = self._key(scope, target, tenant_id)
        kill_reason = _line(reason, "reason", _MAX_REASON_LEN)
        who = _line(actor, "actor", _MAX_ACTOR_LEN)
        if ttl_seconds is not None and (not isinstance(ttl_seconds, int) or ttl_seconds <= 0):
            raise KillSwitchValidationError("ttl_seconds_must_be_positive_int")

        payload = {
            "scope": scope,
            "target": target,
            "tenant_id": tenant_id if scope != "tenant" else None,
            "reason": kill_reason,
            "actor": who,
            "ts": time.time(),
            "ttl_seconds": ttl_seconds,
        }
        client = await self._client()
        try:
            await client.set(key, json.dumps(payload, ensure_ascii=False), ex=ttl_seconds)
        except Exception as exc:  # noqa: BLE001 - Redis 失败必须 fail-closed
            raise KillSwitchUnavailable("redis_set_failed", type(exc).__name__) from exc

        try:
            await self._audit_event(
                "killswitch.enable",
                payload,
                actor=who,
                tenant_id=tenant_id or (target if scope == "tenant" else None),
            )
        except Exception:
            # 审计失败不能留下「已停用但无记录」的状态：回滚开关后向上抛。
            try:
                await client.delete(key)
            except Exception:  # noqa: BLE001 - 回滚失败必须明示
                raise KillSwitchUnavailable("audit_failed_and_rollback_failed") from None
            raise
        return payload

    async def disable(
        self,
        scope: str,
        target: str,
        *,
        actor: str,
        tenant_id: str | None = None,
    ) -> bool:
        """关闭开关；返回是否确实删除了一个活动开关。"""
        key = self._key(scope, target, tenant_id)
        who = _line(actor, "actor", _MAX_ACTOR_LEN)
        client = await self._client()
        try:
            deleted = await client.delete(key)
        except Exception as exc:  # noqa: BLE001
            raise KillSwitchUnavailable("redis_delete_failed", type(exc).__name__) from exc
        await self._audit_event(
            "killswitch.disable",
            {"scope": scope, "target": target, "tenant_id": tenant_id if scope != "tenant" else None},
            actor=who,
            tenant_id=tenant_id or (target if scope == "tenant" else None),
        )
        return bool(deleted)

    async def state(self, scope: str, target: str, *, tenant_id: str | None = None) -> dict[str, Any] | None:
        """返回命中的开关（租户优先，其次全局）；未命中 None。"""
        client = await self._client()
        keys: list[str] = []
        if scope != "tenant" and tenant_id is not None:
            keys.append(self._key(scope, target, tenant_id))
        keys.append(self._key(scope, target, None))
        for key in keys:
            try:
                raw = await client.get(key)
            except Exception as exc:  # noqa: BLE001
                raise KillSwitchUnavailable("redis_get_failed", type(exc).__name__) from exc
            if not raw:
                continue
            try:
                payload = json.loads(raw if isinstance(raw, str) else bytes(raw).decode("utf-8"))
            except (ValueError, TypeError, UnicodeDecodeError) as exc:
                raise KillSwitchUnavailable("redis_payload_corrupt", type(exc).__name__) from exc
            return payload if isinstance(payload, dict) else {"reason": str(payload)}
        return None

    async def check(self, scope: str, target: str, *, tenant_id: str | None = None) -> None:
        """命中则抛 `KillSwitchActive`；未命中静默返回。"""
        hit = await self.state(scope, target, tenant_id=tenant_id)
        if hit is not None:
            raise KillSwitchActive(
                scope=scope,
                target=target,
                reason=str(hit.get("reason") or "disabled"),
                tenant_id=tenant_id,
            )

    async def check_tenant(self, tenant_id: str) -> None:
        await self.check("tenant", tenant_id)

    async def check_model(self, model: str, *, tenant_id: str | None = None) -> None:
        await self.check("model", model, tenant_id=tenant_id)

    async def check_tool(self, tool: str, *, tenant_id: str | None = None) -> None:
        await self.check("tool", tool, tenant_id=tenant_id)

    async def enforce(
        self,
        tenant_id: str,
        *,
        model: str | None = None,
        tool: str | None = None,
    ) -> None:
        """请求入口统一门：租户 → 模型 → 工具，任一命中即 503。"""
        await self.check_tenant(tenant_id)
        if model:
            await self.check_model(model, tenant_id=tenant_id)
        if tool:
            await self.check_tool(tool, tenant_id=tenant_id)

    async def list_active(self, scope: str | None = None) -> list[dict[str, Any]]:
        """列出活动开关（运维总览）。"""
        if scope is not None and scope not in SCOPES:
            raise KillSwitchValidationError(f"scope_must_be_one_of:{','.join(sorted(SCOPES))}")
        client = await self._client()
        pattern = f"{self._key_prefix}{scope}:*" if scope else f"{self._key_prefix}*"
        keys: list[str] = []
        try:
            async for key in client.scan_iter(match=pattern, count=100):
                keys.append(key if isinstance(key, str) else key.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise KillSwitchUnavailable("redis_scan_failed", type(exc).__name__) from exc
        if not keys:
            return []
        try:
            values = await client.mget(*keys)
        except Exception as exc:  # noqa: BLE001
            raise KillSwitchUnavailable("redis_mget_failed", type(exc).__name__) from exc
        active: list[dict[str, Any]] = []
        for key, raw in zip(keys, values or []):
            if not raw:
                continue
            try:
                payload = json.loads(raw if isinstance(raw, str) else bytes(raw).decode("utf-8"))
            except (ValueError, TypeError, UnicodeDecodeError):
                continue
            if isinstance(payload, dict):
                payload["key"] = key
                active.append(payload)
        return active

    async def _audit_event(
        self,
        action: str,
        payload: dict[str, Any],
        *,
        actor: str,
        tenant_id: str | None,
    ) -> None:
        if self._audit is None:
            return
        tenant = tenant_id if tenant_id else _GLOBAL_AUDIT_TENANT
        detail = {
            "scope": payload.get("scope"),
            "target": payload.get("target"),
            "kill_reason": payload.get("reason"),
            "actor": actor,
        }
        await asyncio.to_thread(
            self._audit.append,
            tenant,
            action,
            user_id=actor,
            resource=f"{payload.get('scope')}:{payload.get('target')}",
            detail=detail,
        )


__all__ = [
    "KEY_PREFIX",
    "SCOPES",
    "KillSwitch",
    "KillSwitchActive",
    "KillSwitchError",
    "KillSwitchUnavailable",
    "KillSwitchValidationError",
]
