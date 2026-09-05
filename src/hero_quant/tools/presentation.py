"""工具展示层桩：presentAs native/code/both 的形态分发。

位于 tools 层展示侧，当前保持 native 稳定输出以确保 KV-cache 命中；
code/both 为预留扩展，未来可接入统一渲染。
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List


def _get_field(spec: Any, key: str, default: Any = None) -> Any:
    if isinstance(spec, dict):
        return spec.get(key, default)
    return getattr(spec, key, default)


def present_as_native(spec: Any) -> Dict[str, Any]:
    """返回 OpenAI 兼容的 function 定义，兼容 ToolSpec 与 dict 输入。"""
    name = _get_field(spec, "name", None)
    if not name:
        raise ValueError(f"tool spec missing required 'name': {spec!r}")
    description = _get_field(spec, "description", "")
    parameters = _get_field(spec, "parameters", None)
    if parameters is None:
        parameters = {"type": "object", "properties": {}}
    else:
        # 中文：深拷贝隔离，调用方改返回体不得污染注册表共享状态
        parameters = copy.deepcopy(parameters)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def present_as_code(spec: Any) -> str:
    """返回 code 解释器风格的注释式展示。"""
    name = _get_field(spec, "name", None)
    if not name:
        raise ValueError(f"tool spec missing required 'name': {spec!r}")
    description = _get_field(spec, "description", "")
    # 中文：name 同样逐行注释化（防 name 含换行破坏注释结构，与 description 同规则）
    name_lines = str(name).splitlines() or [""]
    name_commented = "\n".join(f"# Tool: {line}" if line.strip() else "# Tool:" for line in name_lines)
    # 中文：多行 description 逐行加 # 前缀，避免后续行成为未注释文本
    desc_lines = str(description).splitlines() or [""]
    commented = "\n".join(f"# {line}" if line.strip() else "#" for line in desc_lines)
    return f"{name_commented}\n{commented}\n"


def present(spec: Any, presentAs: str = "native") -> Any:
    """按 presentAs 分发单工具的展示形态。"""
    if presentAs == "native":
        return present_as_native(spec)
    if presentAs == "code":
        return present_as_code(spec)
    if presentAs == "both":
        return {
            "native": present_as_native(spec),
            "code": present_as_code(spec),
        }
    raise ValueError(f"unsupported presentAs={presentAs!r}, expected 'native'|'code'|'both'")


def present_definitions(presentAs: str = "native") -> List[Any]:
    """按请求形态返回全量工具定义（桩实现）。"""
    from .registry import TOOL_REGISTRY, _REGISTRY_LOCK, get_definitions

    if presentAs == "native":
        return get_definitions()
    # 中文：持 RLock 遍历，避免并发注册时 dict 变更抛 RuntimeError/KeyError；sorted 保证 KV-cache 稳定
    with _REGISTRY_LOCK:
        specs = [TOOL_REGISTRY[name] for name in sorted(TOOL_REGISTRY.keys())]
    defs: List[Any] = []
    for spec in specs:
        defs.append(present(spec, presentAs=presentAs))
    return defs
