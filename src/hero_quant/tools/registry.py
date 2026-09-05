"""工具注册表：语义化合约、JSON Schema 校验与并发安全标记。

位于 tools 层核心，@tool 装饰器负责将函数注册为 LLM 可调用工具：
- 以 JSON Schema 约束输入/输出，提前校验保证合约稳定；
- is_concurrency_safe(read True / write False) 供调用方做并发审计；
- get_definitions 按名称排序返回，确保 KV-cache 命中稳定；
- presentAs 为展示形态桩（native/code/both），当前保持 native 稳定。
"""

from __future__ import annotations

import copy
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
