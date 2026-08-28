"""工具注册表：语义化合约、JSON Schema 校验与并发安全标记。

位于 tools 层核心，@tool 装饰器负责将函数注册为 LLM 可调用工具：
- 以 JSON Schema 约束输入/输出，提前校验保证合约稳定；
- is_concurrency_safe(read True / write False) 供调用方做并发审计；
- get_definitions 按名称排序返回，确保 KV-cache 命中稳定；
- presentAs 为展示形态桩（native/code/both），当前保持 native 稳定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Any
import inspect
import threading

_REGISTRY_LOCK = threading.RLock()


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
        if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
            raise ValueError(f"{path}: additionalProperties must be bool")
        if "required" in schema:
            if not isinstance(schema["required"], list):
                raise ValueError(f"{path}: required must be list")
            # required entries must be strings and exist in properties
            for idx, req in enumerate(schema["required"]):
                if not isinstance(req, str):
                    raise ValueError(f"{path}.required[{idx}]: must be string")
                if isinstance(props, dict) and req not in props:
                    # allow but warn via error: drift to runtime
                    pass
            # ensure enum/additionalProperties schema etc if present are valid
        if "required" in schema and isinstance(schema.get("required"), list):
            for r in schema["required"]:
                if not isinstance(r, str):
                    raise ValueError(f"{path}: required entries must be strings")
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
    # fallback: truthy
    return lambda args: bool(fn)


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

    # presentAs handling — pop early for unknown-kwargs detection
    present_as_raw = "native"
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
    if output is not None:
        # validate after unwrapping decision — support wrapped form
        if isinstance(output, dict) and "schema" in output and "render" in output:
            assertSupportedJsonSchema(output["schema"])
        else:
            assertSupportedJsonSchema(output)

    def decorator(func: Callable) -> Callable:
        with _REGISTRY_LOCK:
            if name in TOOL_REGISTRY:
                raise ValueError(f"tool name '{name}' already registered")
        if not description:
            raise ValueError("description must be non-empty")

        safe_fn = _normalize_concurrency_safe(is_concurrency_safe)

        # 输出 Schema 统一包为 {schema, render}，便于后续扩展渲染层
        if output is not None:
            if isinstance(output, dict) and "schema" in output and "render" in output:
                output_wrapped: Dict[str, Any] | None = output
            else:
                output_wrapped = {"schema": output, "render": None}
        else:
            output_wrapped = None

        present_as = present_as_raw

        t_ms = None
        if timeoutMs is not None:
            try:
                t_ms = int(timeoutMs)
            except (TypeError, ValueError) as e:
                raise ValueError(f"timeoutMs must be int-convertible, got {timeoutMs!r}") from e
            if t_ms < 0:
                raise ValueError("timeoutMs must be >=0")

        spec = ToolSpec(
            name=name,
            description=description,
            func=func,
            signature=inspect.signature(func),
            parameters=parameters,
            output=output_wrapped,
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
            params = spec.parameters if spec.parameters is not None else {"type": "object", "properties": {}}
            func_def: Dict[str, Any] = {
                "name": spec.name,
                "description": spec.description,
                "parameters": params,
            }
            defs.append({"type": "function", "function": func_def})
        return defs
