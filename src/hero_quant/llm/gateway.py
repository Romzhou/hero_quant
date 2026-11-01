"""Provider 网关：多 provider 顺序回退 + 熔断 + 健康检查（W2 · PR-111）。

职责：把「primary → secondary → ...」的降级链收敛到一个对象，供 AgentLoop /
      `/v1/query` 直接当 `llm` 使用（只需实现 `stream_chat` / `chat`）。

不变量（与 AGENTS §6 fail-closed 对齐）：
- 任一 provider 抛错即记失败并按链回退；全部失败抛 `AllProvidersFailed`，
  **绝不**回退到 `_FallbackChat`/假文本冒充成功；
- 失败原因只记录异常类型/`error_code`，不落 API key、不落完整报文；
- 降级必须可见：`degraded=True` + `degraded_reason`，且计入 `health()` 与指标；
- 流式只在**首个 chunk 之前**允许回退；已产出内容后再失败直接透出
  （重发会重复执行非幂等 RPC，口径同 `llm/client.py`）。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from hero_quant.secrets.resolve import resolve_llm_api_key, secret_value
from hero_quant.telemetry.circuit import CircuitBreaker

from .cost import LLMUsage, build_usage, record_llm_usage

logger = logging.getLogger(__name__)

try:  # 与 llm/client.py 同款
    import structlog as _structlog

    _slog: Any = _structlog.get_logger(__name__)
except Exception:  # pragma: no cover - 最小环境无 structlog
    _slog = None  # type: ignore[assignment]

ALLOWED_PROVIDERS = ("openai", "deepseek", "anthropic")

DEFAULT_PROVIDER_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-chat",
    "anthropic": "claude-3-5-sonnet-latest",
}
"""回退 provider 的默认模型（primary 用 settings.llm_model，不用这张表）。"""

# 逻辑密钥名（PR-151 接线）：实际环境变量由 secrets.env_provider.SECRET_ENV_VARS 登记，
# 这里不再直接 os.environ.get，避免"同一把 key 两套读法"。
_PROVIDER_SECRET_NAMES: dict[str, tuple[str, ...]] = {
    "openai": ("llm_api_key", "openai_api_key"),
    "deepseek": ("deepseek_api_key",),
    "anthropic": ("anthropic_api_key", "claude_api_key"),
}

_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+[A-Za-z0-9._\-]{6,}|api[_-]?key\s*[=:]\s*\S+)", re.IGNORECASE)


def _redact(text: str, limit: int = 120) -> str:
    """脱敏并截断错误描述（防 key/报文进日志与响应）。"""
    body = _SECRET_RE.sub("***", str(text or ""))
    return body[:limit]


class EmptyResponseError(RuntimeError):
    """provider 返回空文本：等同于失败（不得把「什么都没说」当成功）。"""

    code = "empty_response"


class ProviderUnavailable(RuntimeError):
    """provider 客户端构建失败（缺 SDK / 缺 key 等）：按 error code 回退，不落原始报文。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _classify_init_error(exc: BaseException) -> str:
    """把构建期异常映射为稳定 error code（只匹配自家文案，不回显 message）。"""
    if isinstance(exc, ImportError):
        return "sdk_missing"
    message = str(exc)
    if "api_key missing" in message:
        return "api_key_missing"
    if "unknown LLM model" in message:
        return "unknown_model"
    return type(exc).__name__


def _error_code(exc: BaseException) -> str:
    """错误只取「显式 code，否则类型名」，不取 message（message 可能带 key/URL）。"""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return _redact(code, 64)
    return type(exc).__name__


class AllProvidersFailed(RuntimeError):
    """整条 provider 链都不可用（fail-closed：调用方必须显式失败，不得伪造输出）。"""

    def __init__(self, attempts: list[dict[str, str]]):
        self.attempts = list(attempts)
        chain = " -> ".join(f"{a['provider']}({a['error']})" for a in self.attempts) or "<empty chain>"
        super().__init__(f"all llm providers failed: {chain}")


@dataclass(frozen=True)
class ProviderSpec:
    """一个 provider 的静态配置；api_key 永不进 repr/日志。"""

    name: str
    model: str | None = None
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    timeout: int = 30
    priority: int = 0

    def __post_init__(self) -> None:
        name = str(self.name or "").strip().lower()
        if name not in ALLOWED_PROVIDERS:
            raise ValueError(f"unsupported llm provider: {self.name!r} (allowed={ALLOWED_PROVIDERS})")
        object.__setattr__(self, "name", name)

    def safe_dict(self) -> dict[str, Any]:
        """对外可见的配置摘要：has_key 布尔，不含 key 本身。"""
        return {
            "provider": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "timeout": self.timeout,
            "priority": self.priority,
            "has_key": bool(self.api_key),
        }


class _ProviderBreaker:
    """连续失败守卫：N 次连续失败后才把 provider 记入熔断窗口。

    中文：`CircuitBreaker` 以窗口失败率判定，单事件窗口（1 次失败）会立刻 100% 触发。
    对低频的 provider 调用这等于「一次抖动就熔断 15s」，故先用连续失败计数做闸门，
    达到 `trip_after` 后再交给 CircuitBreaker 复盘。
    """

    def __init__(self, breaker: CircuitBreaker, trip_after: int = 2):
        self._breaker = breaker
        self._trip_after = max(1, int(trip_after))
        self._consecutive = 0
        self._tripped = False

    def _sync(self) -> str:
        """让底层 CircuitBreaker 自行推进 OPEN→HALF_OPEN，避免永久拒流。"""
        cur = self._breaker.state
        if self._tripped and cur != "OPEN":
            self._tripped = False
        return cur

    @property
    def state(self) -> str:
        return self._sync()

    def allow(self) -> bool:
        if self._tripped and self._sync() == "OPEN":
            return False
        return self._breaker.allow()

    def record_success(self, duration: float | None = None) -> None:
        self._consecutive = 0
        self._tripped = False
        self._breaker.record_success(duration)

    def record_failure(self, duration: float | None = None) -> None:
        self._consecutive += 1
        if self._consecutive >= self._trip_after:
            self._breaker.record_failure(duration)
            self._tripped = self._breaker.is_open()


@dataclass
class ProviderState:
    """provider 运行时状态（熔断器 + 最近错误 + 成功/失败计数）。"""

    spec: ProviderSpec
    breaker: _ProviderBreaker
    attempts: int = 0
    failures: int = 0
    successes: int = 0
    last_error: str | None = None

    @property
    def state(self) -> str:
        return self.breaker.state

    def snapshot(self) -> dict[str, Any]:
        return {
            "provider": self.spec.name,
            "model": self.spec.model,
            "state": self.state,
            "attempts": self.attempts,
            "failures": self.failures,
            "successes": self.successes,
            "last_error": self.last_error,
            "has_key": bool(self.spec.api_key),
        }


def _default_breaker() -> CircuitBreaker:
    """单 provider 熔断：15s 窗口、50% 失败率、15s 拒流、3 次半开探测。"""
    return CircuitBreaker(failure_threshold=0.5, window=15, open_duration=15, slow_threshold=1.0, half_open_max_calls=3)


class LLMGateway:
    """多 provider 网关：顺序回退 + 每 provider 独立熔断 + 健康检查。

    用法（server 接线留到最后串行，`api/server.py` 不在此线改动）::

        gw = LLMGateway.from_settings(Settings())
        health = gw.health_check()          # 启动时 1 次 ping，失败只标 degraded，不阻塞启动
        loop = AgentLoop(llm=gw, ...)        # 直接当 llm 用
    """

    def __init__(
        self,
        providers: Iterable[ProviderSpec],
        *,
        settings: Any = None,
        client_factory: Callable[[ProviderSpec], Any] | None = None,
        breaker_factory: Callable[[], CircuitBreaker] | None = None,
        trip_after_failures: int = 2,
        clock: Callable[[], float] = time.monotonic,
    ):
        specs = list(providers)
        if not specs:
            raise ValueError("LLMGateway requires at least one provider")
        names = [s.name for s in specs]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate llm provider in chain: {names}")
        # 链序：priority 升序，同优先级保持传入顺序（显式可读，不做隐式字典排序）
        ordered = [s for _, s in sorted(enumerate(specs), key=lambda pair: (pair[1].priority, pair[0]))]
        self._settings = settings
        self._client_factory = client_factory
        self._breaker_factory = breaker_factory or _default_breaker
        self._clock = clock
        self._states: list[ProviderState] = [
            ProviderState(spec=s, breaker=_ProviderBreaker(self._breaker_factory(), trip_after_failures))
            for s in ordered
        ]
        self._clients: dict[str, Any] = {}
        self._active: str | None = None
        self._degraded = False
        self._degraded_reason: str | None = None
        self.last_usage: LLMUsage | None = None
        self.last_tool_calls: list | None = None

    # ------------------------------------------------------------------ 配置
    @classmethod
    def from_settings(
        cls,
        settings: Any = None,
        *,
        include_fallbacks: bool = True,
        provider_models: dict[str, str] | None = None,
        timeout: int = 30,
        client_factory: Callable[[ProviderSpec], Any] | None = None,
        breaker_factory: Callable[[], CircuitBreaker] | None = None,
        trip_after_failures: int = 2,
    ) -> LLMGateway:
        """由 Settings 构造降级链。

        - primary = `settings.llm_provider` + `settings.llm_model`（沿用既有 env 网关，不新增 env）；
        - primary 额外带上 `settings.llm_base_url`（OpenAI 兼容网关根地址）；空则 None 走官方端点。
          回退候选**不**带 base_url —— 别的 provider 不该被 primary 的网关地址串过去；
        - 回退候选 = 其余「显式配了 provider 专属 key」的 provider，**不**把 HERO_API_KEY
          当成通用 key 塞给别家（会把 openai key 打到 deepseek 上）；
        - 无任何可用 key 时仍返回 primary 单链，由首次调用/健康检查如实失败。
        """
        if settings is None:
            from hero_quant.config.settings import Settings

            settings = Settings()
        from .factory import _normalize_provider

        primary = _normalize_provider(getattr(settings, "llm_provider", None))
        primary_model = getattr(settings, "llm_model", None) or DEFAULT_PROVIDER_MODELS.get(primary)
        # PR-151：key 一律经 secrets provider（vault/kms 不可用时不回落 env）
        primary_key, _primary_key_reason = resolve_llm_api_key(settings, provider=primary)

        specs: list[ProviderSpec] = [
            ProviderSpec(
                name=primary,
                model=primary_model,
                api_key=primary_key,
                base_url=getattr(settings, "llm_base_url", None),
                timeout=timeout,
                priority=0,
            )
        ]
        if include_fallbacks:
            models = dict(DEFAULT_PROVIDER_MODELS)
            models.update(provider_models or {})
            for idx, provider in enumerate(ALLOWED_PROVIDERS):
                if provider == primary:
                    continue
                key = _env_key(provider)
                if not key:
                    continue
                specs.append(
                    ProviderSpec(
                        name=provider,
                        model=models.get(provider),
                        api_key=key,
                        timeout=timeout,
                        priority=1 + idx,
                    )
                )
        return cls(
            specs,
            settings=settings,
            client_factory=client_factory,
            breaker_factory=breaker_factory,
            trip_after_failures=trip_after_failures,
        )

    # ------------------------------------------------------------------ 属性
    @property
    def chain(self) -> list[str]:
        return [s.spec.name for s in self._states]

    @property
    def active_provider(self) -> str | None:
        return self._active

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def degraded_reason(self) -> str | None:
        return self._degraded_reason

    # ------------------------------------------------------------------ 内部
    def _client(self, spec: ProviderSpec) -> Any:
        """惰性构建 provider 客户端；构建失败统一为可回退异常。"""
        cached = self._clients.get(spec.name)
        if cached is not None:
            return cached
        if self._client_factory is not None:
            client = self._client_factory(spec)
        else:
            from .factory import LLMFactory

            factory = LLMFactory(settings=self._settings, strict=True)
            try:
                client = factory.create(
                    provider=spec.name,
                    model=spec.model or DEFAULT_PROVIDER_MODELS[spec.name],
                    api_key=spec.api_key,
                    base_url=spec.base_url,
                    timeout=spec.timeout,
                )
            except Exception as exc:  # noqa: BLE001 - 构建失败必须能降级到下一个 provider
                raise ProviderUnavailable(_classify_init_error(exc)) from exc
        if client is None:
            raise RuntimeError(f"client factory returned None for provider {spec.name}")
        self._clients[spec.name] = client
        return client

    def _state(self, name: str) -> ProviderState:
        for st in self._states:
            if st.spec.name == name:
                return st
        raise KeyError(name)

    def _mark_success(self, st: ProviderState, *, skips: list[dict[str, str]], latency: float) -> None:
        st.successes += 1
        st.last_error = None
        st.breaker.record_success()
        self._active = st.spec.name
        if skips:
            self._degraded = True
            first = skips[0]
            self._degraded_reason = f"provider_failover:{first['provider']}->{st.spec.name}:{first['error']}"
            self._inc_failover(first["provider"], st.spec.name, first["error"])
            _log_warning(
                "llm_provider_failover",
                skipped=skips,
                active=st.spec.name,
                latency_ms=round(latency * 1000, 3),
            )
        else:
            self._degraded = False
            self._degraded_reason = None

    def _mark_failure(self, st: ProviderState, exc: BaseException, latency: float) -> dict[str, str]:
        st.attempts += 1
        st.failures += 1
        code = _error_code(exc)
        st.last_error = code
        st.breaker.record_failure(latency)
        _log_warning("llm_provider_failed", provider=st.spec.name, error=code, latency_ms=round(latency * 1000, 3))
        return {"provider": st.spec.name, "error": code}

    @staticmethod
    def _inc_failover(src: str, dst: str, reason: str) -> None:
        try:
            from hero_quant.metrics import inc_llm_failover

            inc_llm_failover(src=src, dst=dst, reason=reason)
        except Exception:  # pragma: no cover - 指标 best-effort（且 W5 可能尚未提供该函数）
            pass

    def _record_usage(
        self,
        st: ProviderState,
        prompt: str,
        *,
        text: str | None,
        output_chars: int | None,
        latency: float,
        client: Any,
    ) -> None:
        usage = build_usage(
            client,
            prompt,
            text,
            provider=st.spec.name,
            model=st.spec.model or DEFAULT_PROVIDER_MODELS.get(st.spec.name, "unknown"),
            latency_s=latency,
            output_chars=output_chars,
        )
        self.last_usage = usage
        record_llm_usage(usage)

    # ------------------------------------------------------------------ 调用
    def chat(self, prompt: str, timeout: int | None = None) -> str:
        """非流式调用；失败按链回退，全失败抛 `AllProvidersFailed`。"""
        response = self.complete(prompt, timeout=timeout)
        return response.text

    def complete(self, prompt: str, timeout: int | None = None) -> "GatewayResponse":
        """非流式调用并返回 provider/用量元数据（评测与审计用）。"""
        skips: list[dict[str, str]] = []
        for st in self._states:
            if not st.breaker.allow():
                skips.append({"provider": st.spec.name, "error": "circuit_open"})
                continue
            t0 = self._clock()
            try:
                client = self._client(st.spec)
                text = client.chat(prompt, timeout=timeout) if timeout is not None else client.chat(prompt)
            except Exception as exc:  # noqa: BLE001 - 任意 provider 故障都必须可回退（错误码已脱敏）
                latency = max(0.0, self._clock() - t0)
                skips.append(self._mark_failure(st, exc, latency))
                continue
            if not str(text or "").strip():
                latency = max(0.0, self._clock() - t0)
                skips.append(self._mark_failure(st, EmptyResponseError("empty_response"), latency))
                continue
            latency = max(0.0, self._clock() - t0)
            self._mark_success(st, skips=skips, latency=latency)
            self._record_usage(st, prompt, text=str(text), output_chars=None, latency=latency, client=client)
            return GatewayResponse(
                text=str(text),
                provider=st.spec.name,
                model=st.spec.model or "",
                degraded=bool(skips),
                degraded_reason=self._degraded_reason,
                latency_s=round(latency, 6),
                attempts=len(skips) + 1,
                usage=self.last_usage,
            )
        self._degraded = True
        self._degraded_reason = "all_providers_failed:" + ",".join(a["provider"] for a in skips)
        raise AllProvidersFailed(skips)

    def stream_chat(self, prompt: str, timeout: int | None = None) -> Iterator[Any]:
        """流式调用；仅在首个 chunk 之前回退（已产出后失败直接透出）。"""
        skips: list[dict[str, str]] = []
        for st in self._states:
            if not st.breaker.allow():
                skips.append({"provider": st.spec.name, "error": "circuit_open"})
                continue
            t0 = self._clock()
            gen: Any = None
            yielded = False
            chars = 0
            try:
                client = self._client(st.spec)
                gen = client.stream_chat(prompt, timeout=timeout) if timeout is not None else client.stream_chat(prompt)
                for chunk in gen:
                    yielded = True
                    chars += _chunk_chars(chunk)
                    yield chunk
            except Exception as exc:  # noqa: BLE001
                latency = max(0.0, self._clock() - t0)
                if yielded:
                    # 已产出内容：重发会重复执行非幂等 RPC，且会把半截输出拼成假完整
                    self._mark_failure(st, exc, latency)
                    raise
                skips.append(self._mark_failure(st, exc, latency))
                continue
            finally:
                _close_quietly(gen)
            latency = max(0.0, self._clock() - t0)
            if not yielded:
                # 空流视为失败（不能把「什么都没说」当成功）
                skips.append(self._mark_failure(st, EmptyResponseError("empty_response"), latency))
                continue
            self._mark_success(st, skips=skips, latency=latency)
            self._record_usage(st, prompt, text=None, output_chars=chars, latency=latency, client=client)
            return
        self._degraded = True
        self._degraded_reason = "all_providers_failed:" + ",".join(a["provider"] for a in skips)
        raise AllProvidersFailed(skips)

    # 兼容 AgentLoop._call_llm 的候选方法名
    def invoke(self, prompt: str) -> str:
        return self.chat(prompt)

    def __call__(self, prompt: str) -> str:
        return self.chat(prompt)

    # ------------------------------------------------------------------ 健康
    def health(self) -> dict[str, Any]:
        """当前链状态快照（不发请求、不浪费配额）。"""
        return {
            "chain": self.chain,
            "active": self._active,
            "degraded": self._degraded,
            "degraded_reason": self._degraded_reason,
            "providers": [st.snapshot() for st in self._states],
        }

    def health_check(self, prompt: str = "ping") -> dict[str, Any]:
        """启动时 1 次 ping：逐 provider 记录 ok/error，不抛错、不阻塞启动。"""
        results: list[dict[str, Any]] = []
        for st in self._states:
            t0 = self._clock()
            entry: dict[str, Any] = {
                "provider": st.spec.name,
                "model": st.spec.model,
                "state": st.state,
                "ok": False,
                "error_code": None,
                "latency_ms": None,
            }
            try:
                client = self._client(st.spec)
                text = client.chat(prompt)
                entry["latency_ms"] = round(max(0.0, self._clock() - t0) * 1000, 3)
                if str(text or "").strip():
                    entry["ok"] = True
                    st.breaker.record_success()
                    if self._active is None:
                        self._active = st.spec.name
                else:
                    entry["error_code"] = "empty_response"
                    st.breaker.record_failure(max(0.0, self._clock() - t0))
            except Exception as exc:  # noqa: BLE001 - 健康检查必须收敛所有异常
                entry["error_code"] = _error_code(exc)
                entry["latency_ms"] = round(max(0.0, self._clock() - t0) * 1000, 3)
                st.breaker.record_failure(max(0.0, self._clock() - t0))
                st.last_error = entry["error_code"]
            results.append(entry)

        ok_any = any(r["ok"] for r in results)
        primary_ok = bool(results and results[0]["ok"])
        self._degraded = ok_any and not primary_ok
        if self._degraded:
            first_ok = next(r["provider"] for r in results if r["ok"])
            self._degraded_reason = f"provider_failover:{results[0]['provider']}->{first_ok}"
        elif not ok_any:
            self._degraded_reason = "all_providers_failed:health_check"
        else:
            self._degraded_reason = None
        return {
            "ok": ok_any,
            "degraded": self._degraded,
            "degraded_reason": self._degraded_reason,
            "active": self._active,
            "providers": results,
        }


@dataclass(frozen=True)
class GatewayResponse:
    """非流式调用的结构化结果。"""

    text: str
    provider: str
    model: str
    degraded: bool = False
    degraded_reason: str | None = None
    latency_s: float | None = None
    attempts: int = 1
    usage: LLMUsage | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "latency_s": self.latency_s,
            "attempts": self.attempts,
            "usage": self.usage.to_dict() if self.usage is not None else None,
        }


def _env_key(provider: str) -> str | None:
    """读 provider 专属 key（经 secrets provider；空值视为未配置）；不落日志、不落值。"""
    for name in _PROVIDER_SECRET_NAMES.get(provider, ()):
        val, _reason = secret_value(name)
        if val:
            return val
    return None


def _chunk_chars(chunk: Any) -> int:
    """统计流式 chunk 的文本长度（用于成本估算），忽略结构性 chunk。"""
    if isinstance(chunk, str):
        return len(chunk)
    if isinstance(chunk, dict):
        text = chunk.get("text")
        return len(text) if isinstance(text, str) else 0
    content = getattr(chunk, "content", None)
    return len(content) if isinstance(content, str) else 0


def _close_quietly(gen: Any) -> None:
    """尽力关闭上游生成器（不掩盖主异常）。"""
    if gen is None:
        return
    close = getattr(gen, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # pragma: no cover - 收尾 best-effort
            pass


def _log_warning(op: str, **fields: Any) -> None:
    """结构化告警：只带脱敏后的 provider/error/延迟，不带 prompt 与 key。"""
    try:
        logger.warning("%s %s", op, fields)
    except Exception:  # pragma: no cover
        pass
    if _slog is None:
        return
    try:
        _slog.warning(op, chain="A", **fields)
    except Exception:  # pragma: no cover
        pass


__all__ = [
    "ALLOWED_PROVIDERS",
    "AllProvidersFailed",
    "DEFAULT_PROVIDER_MODELS",
    "EmptyResponseError",
    "GatewayResponse",
    "LLMGateway",
    "ProviderSpec",
    "ProviderState",
    "ProviderUnavailable",
]
