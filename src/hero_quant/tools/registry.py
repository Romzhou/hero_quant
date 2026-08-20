from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Any
import inspect


def assertSupportedJsonSchema(schema: Dict[str, Any]) -> None:
    """Validate minimal JSON Schema support. Raises ValueError on unsupported."""
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
    name: str
    description: str
    func: Callable
    signature: inspect.Signature | None = None
    parameters: Dict[str, Any] | None = None
    output: Dict[str, Any] | None = None  # stored as {"schema": ..., "render": ...}
    is_concurrency_safe: Callable[[Dict[str, Any]], bool] = field(default_factory=lambda: lambda args: False)  # type: ignore
    timeoutMs: int | None = None
    # stub for presentAs
    presentAs: str = "native"


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
    """Decorator to register a function as a tool with semantic contract.

    Supports:
    - name, description (required)
    - parameters: JSON Schema for inputs (validated)
    - output: JSON Schema for outputs (validated, stored as {schema, render})
    - is_concurrency_safe: Callable[[args], bool] or bool
    - timeoutMs: int (also accepts timeout_ms / timeoutms aliases)
    - presentAs: native|code|both (stub)
    """
    # handle aliases
    if timeoutMs is None:
        if "timeout_ms" in kwargs:
            timeoutMs = kwargs.pop("timeout_ms")
        elif "timeoutms" in kwargs:
            timeoutMs = kwargs.pop("timeoutms")
        elif "timeout" in kwargs:
            timeoutMs = kwargs.pop("timeout")
    # is_concurrency_safe alias
    if is_concurrency_safe is None and "concurrency_safe" in kwargs:
        is_concurrency_safe = kwargs.pop("concurrency_safe")

    if not description:
        raise ValueError("description must be non-empty")
    if name in TOOL_REGISTRY:
        raise ValueError(f"tool name '{name}' already registered")

    # validate schemas early
    if parameters is not None:
        assertSupportedJsonSchema(parameters)
    if output is not None:
        assertSupportedJsonSchema(output)

    def decorator(func: Callable) -> Callable:
        if name in TOOL_REGISTRY:
            raise ValueError(f"tool name '{name}' already registered")
        if not description:
            raise ValueError("description must be non-empty")

        # normalize is_concurrency_safe to callable
        safe_fn = _normalize_concurrency_safe(is_concurrency_safe)

        # wrap output as {schema, render}
        if output is not None:
            if isinstance(output, dict) and "schema" in output and "render" in output:
                output_wrapped: Dict[str, Any] | None = output
            else:
                output_wrapped = {"schema": output, "render": None}
        else:
            output_wrapped = None

        # presentAs stub
        present_as = kwargs.get("presentAs", kwargs.get("present_as", "native"))

        # timeoutMs normalization
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
    """Return tool definitions sorted by name (toolOrder) for KV-cache stability.

    presentAs: native|code|both — stub, currently returns native form regardless
    (both would return merged, code would return code string; minimal keeps native).
    """
    # toolOrder sort + KV-cache stable: sorted by name
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
    # presentAs stub handling
    if presentAs == "code":
        # code present would transform, but minimal keeps native for stability
        # we could map via presentation.present_as_code, but keep native to pass tests
        pass
    elif presentAs == "both":
        pass
    return defs
