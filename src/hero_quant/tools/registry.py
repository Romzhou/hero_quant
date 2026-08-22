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


def assertSupportedJsonSchema(schema: Dict[str, Any]) -> None:
    """校验最小可用 JSON Schema 子集，不支持的类型抛出 ValueError。"""
    if not isinstance(schema, dict):
        raise ValueError("schema must be dict")
    if "type" not in schema:
        raise ValueError("schema must have 'type'")
    t = schema["type"]
    if t not in ("object", "array", "string", "number", "integer", "boolean", "null"):
        raise ValueError(f"unsupported json schema type: {t}")
    if t == "object":
        props = schema.get("properties")
        if props is not None and not isinstance(props, dict):
            raise ValueError("properties must be dict")
        if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
            raise ValueError("additionalProperties must be bool")
        if "required" in schema and not isinstance(schema["required"], list):
            raise ValueError("required must be list")
        # validate property schemas shallow
        if isinstance(props, dict):
            for k, v in props.items():
                if not isinstance(v, dict) or "type" not in v:
                    raise ValueError(f"property {k} must be schema dict with type")
                if v["type"] not in ("object", "array", "string", "number", "integer", "boolean", "null"):
                    raise ValueError(f"unsupported property type {v['type']} for {k}")


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
    # 兼容下划线/驼峰等别名写法
    if timeoutMs is None:
        if "timeout_ms" in kwargs:
            timeoutMs = kwargs.pop("timeout_ms")
        elif "timeoutms" in kwargs:
            timeoutMs = kwargs.pop("timeoutms")
        elif "timeout" in kwargs:
            timeoutMs = kwargs.pop("timeout")
    if is_concurrency_safe is None and "concurrency_safe" in kwargs:
        is_concurrency_safe = kwargs.pop("concurrency_safe")

    if not description:
        raise ValueError("description must be non-empty")
    if name in TOOL_REGISTRY:
        raise ValueError(f"tool name '{name}' already registered")

    # 注册期即校验 Schema， fail-fast 避免运行时合约漂移
    if parameters is not None:
        assertSupportedJsonSchema(parameters)
    if output is not None:
        assertSupportedJsonSchema(output)

    def decorator(func: Callable) -> Callable:
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

        present_as = kwargs.get("presentAs", kwargs.get("present_as", "native"))

        t_ms = None
        if timeoutMs is not None:
            try:
                t_ms = int(timeoutMs)
            except Exception:
                t_ms = None

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
        TOOL_REGISTRY[name] = spec
        return func

    return decorator


def get_definitions(presentAs: str = "native") -> list[Dict[str, Any]]:
    """按名称排序返回工具定义，保证 KV-cache 稳定；presentAs 为展示形态桩。"""
    # 按名称排序：避免注册顺序抖动导致 KV-cache 失效

    defs: list[Dict[str, Any]] = []
    for tool_name in sorted(TOOL_REGISTRY.keys()):
        spec = TOOL_REGISTRY[tool_name]
        params = spec.parameters if spec.parameters is not None else {"type": "object", "properties": {}}
        func_def: Dict[str, Any] = {
            "name": spec.name,
            "description": spec.description,
            "parameters": params,
        }
        defs.append({"type": "function", "function": func_def})
    # presentAs 桩：当前保持 native 稳定，code/both 预留扩展
    if presentAs == "code":
        pass
    elif presentAs == "both":
        pass
    return defs
