"""工具注册表：语义化合约、JSON Schema 校验与并发安全标记。

位于 tools 层核心，@tool 装饰器负责将函数注册为 LLM 可调用工具：
- 以 JSON Schema 约束输入/输出，提前校验保证合约稳定；
- is_concurrency_safe(read True / write False) 供调用方做并发审计；
- get_definitions 按名称排序返回，确保 KV-cache 命中稳定；
- presentAs 为展示形态桩（native/code/both），当前保持 native 稳定；
- execute_tool_call 是 AgentLoop 工具路径的显式执行器，可选接入 DedupStore 幂等去重；
  mcp/server.py 有独立适配器调用点，不走本执行器；dedup_store=None 时与 spec.func(**args) 等价。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Dict, Any
import inspect
import threading

if TYPE_CHECKING:  # pragma: no cover — 仅类型检查期引入，运行期零依赖（避免 governance↔tools 循环导入）
    from hero_quant.governance.dedup import DedupStore

_REGISTRY_LOCK = threading.RLock()

_logger = logging.getLogger(__name__)

try:
    import structlog as _structlog  # type: ignore

    _slog = _structlog.get_logger(__name__)
except Exception:  # pragma: no cover — 最小环境无 structlog
    _slog = None  # type: ignore


def _assert_schema(schema: Dict[str, Any], path: str = "$") -> None:
    """Recursively validate JSON Schema subset."""
    if not isinstance(schema, dict):
        raise ValueError(f"{path}: schema must be dict")
    if "type" not in schema:
        raise ValueError(f"{path}: schema must have 'type'")
    t = schema["type"]
    if t not in ("object", "array", "string", "number", "integer", "boolean", "null"):
        raise ValueError(f"{path}: unsupported json schema type: {t}")
    if t == "object":
        props = schema.get("properties")
        if props is not None and not isinstance(props, dict):
            raise ValueError(f"{path}: properties must be dict")
        # 中文：additionalProperties 允许 bool 或 schema dict（合法 JSON Schema），否则拒收
        if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], (bool, dict)):
            raise ValueError(f"{path}: additionalProperties must be bool or schema dict")
        if "required" in schema:
            if not isinstance(schema["required"], list):
                raise ValueError(f"{path}: required must be list")
            # 中文：required 条目必须为字符串（类型校验）；存在性不做强制，避免误拒演进中的 schema
            for idx, req in enumerate(schema["required"]):
                if not isinstance(req, str):
                    raise ValueError(f"{path}.required[{idx}]: must be string")
            # ensure enum/additionalProperties schema etc if present are valid
        if isinstance(props, dict):
            for k, v in props.items():
                _assert_schema(v, f"{path}.properties.{k}")
        # additionalProperties as schema object case
        ap = schema.get("additionalProperties")
        if isinstance(ap, dict):
            _assert_schema(ap, f"{path}.additionalProperties")
    elif t == "array":
        if "items" in schema:
            items = schema["items"]
            if isinstance(items, dict):
                _assert_schema(items, f"{path}.items")
            elif isinstance(items, list):
                for i, it in enumerate(items):
                    _assert_schema(it, f"{path}.items[{i}]")


def assertSupportedJsonSchema(schema: Dict[str, Any]) -> None:
    """校验最小可用 JSON Schema 子集，不支持的类型抛出 ValueError。"""
    _assert_schema(schema, "$")


def _normalize_concurrency_safe(fn: Callable | bool | None) -> Callable[[Dict[str, Any]], bool]:
    """将 bool/Callable 统一为 Callable[[args], bool]，默认 False（保守写安全）。"""
    if fn is None:
        return lambda args: False
    if callable(fn):
        return fn  # type: ignore[return-value]
    if isinstance(fn, bool):
        val = fn
        return lambda args, v=val: v
    # 中文：非 bool/非 callable（如字符串 "false"）一律 fail-fast，禁止 truthy 隐式标记安全
    raise ValueError(f"is_concurrency_safe must be bool/Callable/None, got {fn!r}")


@dataclass
class ToolSpec:
    """单工具的语义化合约与运行时元数据。"""

    name: str
    description: str
    func: Callable
    signature: inspect.Signature | None = None
    parameters: Dict[str, Any] | None = None
    output: Dict[str, Any] | None = None  # 统一存为 {"schema": ..., "render": ...}
    is_concurrency_safe: Callable[[Dict[str, Any]], bool] = field(default_factory=lambda: lambda args: False)  # type: ignore
    timeoutMs: int | None = None
    presentAs: str = "native"  # 展示形态桩，预留 code/both


TOOL_REGISTRY: Dict[str, ToolSpec] = {}


def tool(
    name: str,
    description: str,
    parameters: Dict[str, Any] | None = None,
    output: Dict[str, Any] | None = None,
    is_concurrency_safe: Callable | bool | None = None,
    timeoutMs: int | None = None,
    **kwargs: Any,
):
    """注册函数为工具，固化语义化合约与并发安全标记。

    约定：只读工具 is_concurrency_safe 为 True，有状态写为 False；
    parameters/output 为 JSON Schema，注册期即校验；timeoutMs 与
    presentAs 为可选扩展，支持下划线/驼峰别名兼容。
    """
    # 兼容下划线/驼峰等别名写法 — detect conflicting aliases
    timeout_aliases = []
    if "timeout_ms" in kwargs:
        timeout_aliases.append("timeout_ms")
    if "timeoutms" in kwargs:
        timeout_aliases.append("timeoutms")
    if "timeout" in kwargs:
        timeout_aliases.append("timeout")
    # 中文：显式 timeoutMs + 任一别名同属冲突（此前仅统计 kwargs 别名，显式参数被漏检）
    if timeoutMs is not None and timeout_aliases:
        raise ValueError(f"conflicting timeout aliases: ['timeoutMs', {timeout_aliases}]")
    if len(timeout_aliases) > 1:
        raise ValueError(f"conflicting timeout aliases: {timeout_aliases}")
    if timeoutMs is None:
        if "timeout_ms" in kwargs:
            timeoutMs = kwargs.pop("timeout_ms")
        elif "timeoutms" in kwargs:
            timeoutMs = kwargs.pop("timeoutms")
        elif "timeout" in kwargs:
            timeoutMs = kwargs.pop("timeout")
    if is_concurrency_safe is None and "concurrency_safe" in kwargs:
        is_concurrency_safe = kwargs.pop("concurrency_safe")
    # 中文：显式 is_concurrency_safe + 别名 concurrency_safe 同属冲突（与 timeout 别名同规则）
    if "concurrency_safe" in kwargs:
        raise ValueError("conflicting concurrency_safe aliases: ['is_concurrency_safe', 'concurrency_safe']")

    # presentAs handling — pop early for unknown-kwargs detection
    present_as_raw = "native"
    if "presentAs" in kwargs and "present_as" in kwargs:
        raise ValueError("conflicting presentAs aliases: ['presentAs', 'present_as']")
    if "presentAs" in kwargs:
        present_as_raw = kwargs.pop("presentAs")
    elif "present_as" in kwargs:
        present_as_raw = kwargs.pop("present_as")
    if present_as_raw not in ("native", "code", "both"):
        raise ValueError(f"unsupported presentAs={present_as_raw!r}")

    # fail-fast on typos / unknown kwargs
    if kwargs:
        raise ValueError(f"unknown tool() kwargs: {list(kwargs)}")

    if not description:
        raise ValueError("description must be non-empty")
    with _REGISTRY_LOCK:
        if name in TOOL_REGISTRY:
            raise ValueError(f"tool name '{name}' already registered")

    # 注册期即校验 Schema， fail-fast 避免运行时合约漂移
    if parameters is not None:
        assertSupportedJsonSchema(parameters)

    def decorator(func: Callable) -> Callable:
        with _REGISTRY_LOCK:
            if name in TOOL_REGISTRY:
                raise ValueError(f"tool name '{name}' already registered")
        if not description:
            raise ValueError("description must be non-empty")
        # 中文：对深拷贝后的输入/输出合约做使用时校验（防 tool(...) 与 dec(func) 之间被篡改）；
        # 仅精确 {"schema", "render"} 视为 wrapped 形态（validate output["schema"] when wrapped），
        # 避免原始 schema 误判。
        if parameters is not None:
            parameters_snapshot = copy.deepcopy(parameters)
            assertSupportedJsonSchema(parameters_snapshot)
        else:
            parameters_snapshot = None
        if output is not None:
            output_snapshot = copy.deepcopy(output)
            if isinstance(output_snapshot, dict) and set(output_snapshot) == {"schema", "render"}:
                assertSupportedJsonSchema(output_snapshot["schema"])
                output_wrapped: Dict[str, Any] | None = output_snapshot
            else:
                assertSupportedJsonSchema(output_snapshot)
                output_wrapped = {"schema": output_snapshot, "render": None}
        else:
            output_wrapped = None

        safe_fn = _normalize_concurrency_safe(is_concurrency_safe)

        present_as = present_as_raw

        t_ms = None
        if timeoutMs is not None:
            # 中文：bool 是 int 子类（True==1），必须先拒收；float 仅接受整数值，禁止截断
            if isinstance(timeoutMs, bool):
                raise ValueError(f"timeoutMs must be int, got {timeoutMs!r}")
            if isinstance(timeoutMs, float):
                if not timeoutMs.is_integer():
                    raise ValueError(f"timeoutMs must be integer, got {timeoutMs!r}")
                t_ms = int(timeoutMs)
            else:
                try:
                    t_ms = int(timeoutMs)
                except (TypeError, ValueError) as e:
                    raise ValueError(f"timeoutMs must be int-convertible, got {timeoutMs!r}") from e
                if isinstance(timeoutMs, str) and str(t_ms) != timeoutMs.strip():
                    # "5.0"/" 5 " 等非纯整数字符串：int() 截断/容错会掩盖误配，显式拒绝
                    try:
                        _f = float(timeoutMs.strip())
                    except (TypeError, ValueError):
                        _f = None
                    if _f is None or not float(_f).is_integer() or int(_f) != t_ms:
                        raise ValueError(f"timeoutMs must be integer, got {timeoutMs!r}")
            if t_ms < 0:
                raise ValueError("timeoutMs must be >=0")

        spec = ToolSpec(
            name=name,
            description=description,
            func=func,
            signature=inspect.signature(func),
            # 中文：存使用时快照（已深拷贝+重校验），事后改原 dict 不得漂移已注册合约
            parameters=parameters_snapshot,
            output=copy.deepcopy(output_wrapped),
            is_concurrency_safe=safe_fn,
            timeoutMs=t_ms,
            presentAs=present_as,
        )
        with _REGISTRY_LOCK:
            if name in TOOL_REGISTRY:
                raise ValueError(f"tool name '{name}' already registered")
            TOOL_REGISTRY[name] = spec
        return func

    return decorator


def get_definitions(presentAs: str = "native") -> list[Dict[str, Any]]:
    """按名称排序返回工具定义，保证 KV-cache 稳定；presentAs 为展示形态桩。"""
    # 按名称排序：避免注册顺序抖动导致 KV-cache 失效
    if presentAs not in ("native", "code", "both"):
        raise ValueError(f"unsupported presentAs={presentAs!r}")
    if presentAs != "native":
        raise NotImplementedError(f"presentAs={presentAs!r} not yet implemented")

    with _REGISTRY_LOCK:
        items = sorted(TOOL_REGISTRY.items())
        defs: list[Dict[str, Any]] = []
        for tool_name, spec in items:
            # 中文：深拷贝导出，调用方改返回体不得污染注册表共享状态
            params = copy.deepcopy(spec.parameters) if spec.parameters is not None else {"type": "object", "properties": {}}
            func_def: Dict[str, Any] = {
                "name": spec.name,
                "description": spec.description,
                "parameters": params,
            }
            defs.append({"type": "function", "function": func_def})
        return defs


# ---------------------------------------------------------------------------
# P0-4 幂等接线：显式工具执行器
# ---------------------------------------------------------------------------
# 背景：governance.dedup.DedupStore 早已实现 PENDING→SUCCESS/FAILED 状态机，但此前
# 无任何入口能到达它（L1 未接线），工具重试即重复副作用。本区块提供 AgentLoop 工具路径
# 的执行器 execute_tool_call；@tool 装饰器保持原样，默认路径零影响。
_CHAIN = "A"  # F线分级：本文件位于问答链工具调度层（漏接去重即重复执行副作用）
_RESULT_KEY = "__dedup_value__"
_DEFAULT_TENANT = "default"
_DEFAULT_WORKFLOW = "adhoc"
_DEFAULT_STEP = "s1"
_KEY_PART_MAX = 64


def _warn(op: str, msg: str, *args: Any, **fields: Any) -> None:
    """F线分级告警（与 dedup.py / market_data.py 同款 schema）：stdlib warning 为主，
    structlog 结构化为辅（error_code + chain；trace_id 由 server.py contextvars 透传）。
    PII 约束：只传工具名/幂等键/类型名，禁参数与结果原文。"""
    _exc_info = fields.pop("exc_info", None)
    try:
        _logger.warning(msg, *args, exc_info=_exc_info)
    except Exception:
        pass
    if _slog is None:
        return
    try:
        _slog.warning(op, error_code=op, chain=fields.pop("chain", _CHAIN), exc_info=_exc_info, **fields)
    except Exception:
        pass


def _clean_part(value: Any) -> str:
    """清洗幂等键分段：冒号/空白/不可打印字符替换为下划线并截断。

    derive_key 只接受 ^[^:]+$ 的分段，直接透传用户可控入参（business_id 等）会抛
    ValueError 把正常工具调用打成异常，故这里统一清洗；清洗后为空由调用方回落缺省值。
    """
    raw = "" if value is None else str(value).strip()
    cleaned = "".join(ch if (ch.isprintable() and not ch.isspace() and ch != ":") else "_" for ch in raw)
    return cleaned.strip("_")[:_KEY_PART_MAX]


def _args_digest(args: Dict[str, Any]) -> str:
    """业务 id 缺省时的参数指纹：同工具同参数 = 同一业务，重试天然命中同一 key。"""
    try:
        canon = json.dumps(args, sort_keys=True, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        canon = repr(sorted((str(k), repr(v)) for k, v in args.items()))
    return "args-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _tenant_or_default(tenant: str | None) -> str:
    """租户分段：显式入参 > auth.context.current_tenant_id() > "default"。

    离线 / 无租户上下文（CLI、后台任务、单测）不得抛 MissingTenant 打断工具调用，但也不能
    静默：取不到时留 warning 并回落 "default"（该键与所有无租户调用共享命名空间，仅作兜底）。
    """
    explicit = _clean_part(tenant)
    if explicit:
        return explicit
    try:
        from hero_quant.auth.context import current_tenant_id

        tid = current_tenant_id()
    except Exception as exc:  # 中文：最小环境无 auth 属正常降级，但必须留痕
        _warn("dedup_tenant_lookup_failed", "tool executor tenant lookup failed: %s", exc, exc_info=exc)
        return _DEFAULT_TENANT
    return _clean_part(tid) or _DEFAULT_TENANT


def _derive_call_key(
    spec: "ToolSpec",
    args: Dict[str, Any],
    *,
    tenant: str | None,
    workflow_id: str | None,
    step_id: str | None,
    business_id: str | None,
) -> str:
    """按 {tenant}:{workflow}:{step}:{tool}:{businessId} 派生幂等键。

    取值优先级（逐段）：
    - tenant：显式 tenant 入参 > auth.context.current_tenant_id() > "default"；
    - workflow_id / step_id / business_id：显式入参 > args 中的同名键 > 缺省值，
      缺省值依次为 "adhoc" / "s1" / 参数指纹（同参数即同一业务）。

    所有分段经 _clean_part 清洗（冒号→下划线、去空白、截断 64 字符），因此入参含冒号不会
    触发 derive_key 的 ValueError；清洗后为空的段再回落缺省值。
    """
    from hero_quant.governance.dedup import derive_key

    wf = _clean_part(workflow_id if workflow_id is not None else args.get("workflow_id")) or _DEFAULT_WORKFLOW
    st = _clean_part(step_id if step_id is not None else args.get("step_id")) or _DEFAULT_STEP
    biz_raw = business_id if business_id is not None else args.get("business_id")
    biz = _clean_part(biz_raw) or _args_digest(args)
    return derive_key(_tenant_or_default(tenant), wf, st, _clean_part(spec.name) or "tool", biz)


def _unwrap_result(stored: Any) -> Any:
    """拆结果信封；非信封（历史裸 JSON 记录）原样返回，保证向前兼容。"""
    if isinstance(stored, dict) and _RESULT_KEY in stored:
        return stored[_RESULT_KEY]
    return stored


def _wrap_result(result: Any, tool: str, key: str) -> Dict[str, Any]:
    """把结果包成可 JSON 序列化的信封。

    Why 信封：DedupStore.mark_success 对 str 结果存原文、对其它类型存 json 串，get() 统一
    json.loads —— 于是字符串 "123" 回放会变成 int 123、'{"a":1}' 会变成 dict。固定信封
    {"__dedup_value__": ...} 让回放类型精确。不可序列化时返回带 degraded 的描述符
    （绝不返回 None 冒充成功）。
    """
    try:
        return {_RESULT_KEY: json.loads(json.dumps(result, ensure_ascii=False))}
    except (TypeError, ValueError) as exc:
        _warn(
            "dedup_result_not_json_serializable",
            "tool result not JSON serializable, dedup replay degrades to descriptor: tool=%s key=%s type=%s err=%s",
            tool,
            key,
            type(result).__name__,
            exc,
        )
        return {
            "degraded": True,
            "reason": "result_not_json_serializable",
            "tool": tool,
            "result_type": type(result).__name__,
            "result_repr": repr(result)[:200],
        }


def _replay_or_raise(store: "DedupStore", key: str, tool: str, wait_timeout: float) -> Any:
    """占位冲突时的 WAIT 语义：SUCCESS 直接重放，FAILED/PENDING 一律 fail-closed 抛错。"""
    rec = store.wait_for(key, timeout=wait_timeout)
    status = str((rec or {}).get("status") or "").upper()
    if status == "SUCCESS":
        return _unwrap_result((rec or {}).get("result"))
    if status == "FAILED":
        raise RuntimeError(
            f"dedup_conflict_failed: tool={tool} key={key} "
            f"previous_error={(rec or {}).get('error')!r}; replay refused (fail-closed)"
        )
    raise RuntimeError(
        f"dedup_conflict_pending_timeout: tool={tool} key={key} "
        f"status={status or 'MISSING'} wait_timeout={wait_timeout}s; tool NOT executed (fail-closed)"
    )


def execute_tool_call(
    spec: "ToolSpec",
    args: Dict[str, Any] | None = None,
    *,
    dedup_store: "DedupStore | None" = None,
    idempotency_key: str | None = None,
    tenant: str | None = None,
    workflow_id: str | None = None,
    step_id: str | None = None,
    business_id: str | None = None,
    wait_timeout: float = 5.0,
) -> Any:
    """执行工具调用（唯一副作用入口），可选经 DedupStore 做幂等去重。

    语义：
    - dedup_store is None：直接 spec.func(**args)，与改造前完全一致（默认零影响）；
    - 有 store 且未给 idempotency_key：按 _derive_call_key 派生；
    - insert_pending 为 True（成功 claim）：才执行工具，成功后 mark_success、异常则
      mark_failed 后原样抛出；
    - insert_pending 为 False（已存在同键记录）：走 wait_for —— SUCCESS 直接返回首次结果
      （不执行工具）、FAILED 抛 RuntimeError、PENDING 超时抛 RuntimeError。

    不变量：未成功 claim 时绝不执行工具，因此任何并发/重放路径都不会产生第二次副作用。
    工具已成功但记账失败（mark_success 抛错）只降级告警并返回真实结果：键留 PENDING，等
    TTL 自愈，绝不把成功伪装成失败。

    警告（命名空间）：不传 workflow_id / step_id 时键会落到 "adhoc" / "s1" 命名空间，
    即同一租户内「同工具 + 同参数」的调用在 TTL 内会互相去重（跨会话也命中）。生产接线
    必须显式传 workflow_id（如 checkpoint_thread）与 step_id，并用 business_id 编码业务
    意图，否则会把「合法的第二次相同调用」误当成重放。
    """
    call_args: Dict[str, Any] = dict(args) if args else {}
    if dedup_store is None:
        return spec.func(**call_args)

    # 中文：工具名同样清洗后使用，避免空/含冒号 name 让 insert_pending 直接 ValueError
    tool_name = _clean_part(spec.name) or "tool"
    key = idempotency_key or _derive_call_key(
        spec, call_args, tenant=tenant, workflow_id=workflow_id, step_id=step_id, business_id=business_id
    )
    if not dedup_store.insert_pending(key, tool_name):
        # 中文：未拿到 claim 一律不执行；只有 SUCCESS 终态才允许重放结果
        return _replay_or_raise(dedup_store, key, tool_name, wait_timeout)

    try:
        result = spec.func(**call_args)
    except Exception as exc:
        try:
            dedup_store.mark_failed(key, f"{type(exc).__name__}: {exc}")
        except Exception as mark_exc:  # 中文：标记失败不得掩盖工具原始异常
            _warn(
                "dedup_mark_failed_failed",
                "dedup mark_failed raised, original tool error wins: tool=%s key=%s err=%s",
                spec.name,
                key,
                mark_exc,
                exc_info=mark_exc,
            )
        raise

    try:
        dedup_store.mark_success(key, _wrap_result(result, spec.name, key))
    except Exception as mark_exc:  # 中文：工具已成功，记账失败只降级告警（键留 PENDING，TTL 后自愈）
        _warn(
            "dedup_mark_success_failed",
            "dedup mark_success raised; key stays PENDING until TTL: tool=%s key=%s err=%s",
            spec.name,
            key,
            mark_exc,
            exc_info=mark_exc,
        )
    return result
