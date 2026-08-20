"""Tool presentation stub — presentAs native|code|both.

Minimal stub for Wave A6. Future: KV-cache stable rendering for function calling.
"""

from __future__ import annotations

from typing import Any, Dict, List


def present_as_native(spec: Any) -> Dict[str, Any]:
    """Return OpenAI-native function definition for a ToolSpec."""
    # Support both ToolSpec dataclass and dict
    name = getattr(spec, "name", spec.get("name") if isinstance(spec, dict) else "unknown")
    description = getattr(spec, "description", spec.get("description") if isinstance(spec, dict) else "")
    parameters = getattr(spec, "parameters", None)
    if parameters is None and isinstance(spec, dict):
        parameters = spec.get("parameters")
    if parameters is None:
        parameters = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def present_as_code(spec: Any) -> str:
    """Return code-style presentation (e.g. for code interpreter)."""
    name = getattr(spec, "name", spec.get("name") if isinstance(spec, dict) else "unknown")
    description = getattr(spec, "description", "")
    return f"# Tool: {name}\n# {description}\n"


def present(spec: Any, presentAs: str = "native") -> Any:
    """Present a tool in native, code, or both forms."""
    if presentAs == "native":
        return present_as_native(spec)
    if presentAs == "code":
        return present_as_code(spec)
    if presentAs == "both":
        return {
            "native": present_as_native(spec),
            "code": present_as_code(spec),
        }
    # fallback
    return present_as_native(spec)


def present_definitions(presentAs: str = "native") -> List[Dict[str, Any]]:
    """Return all registered tools presented in requested form (stub)."""
    from .registry import TOOL_REGISTRY, get_definitions

    if presentAs == "native":
        return get_definitions()
    # For code/both, map via present()
    defs: List[Dict[str, Any]] = []
    for spec in TOOL_REGISTRY.values():
        defs.append(present(spec, presentAs=presentAs))
    return defs
