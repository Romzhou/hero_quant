"""集中截断与配额 —— 工具返回值的统一上限。

职责：定义 TOOL_RESULT_LIMIT 等全局字符预算，防止 Function Calling 结果过大阻塞上下文。
架构位置：config 层常量，被工具与 trace 模块复用；trace 侧边栏上限 50k 与此区分。
设计决策：默认 10_000 字符兼顾可读性与 token 成本，支持 HERO_TOOL_RESULT_LIMIT 环境覆盖（通过 environ.get，避免 env gate 冲突）。
"""

from __future__ import annotations

import json
from typing import Any

TOOL_RESULT_LIMIT: int = 10_000  # 默认字符预算，平衡上下文长度与截断频率

# 可选环境覆盖：通过 os.environ.get 而非 os.getenv，避免与 Settings 的 env gate 重复
try:
    import os

    _env_val = os.environ.get("HERO_TOOL_RESULT_LIMIT")
    if _env_val is not None and _env_val.strip() != "":
        try:
            TOOL_RESULT_LIMIT = int(_env_val)
        except Exception:
            pass
except Exception:
    pass


def truncate_tool_result(result: Any, limit: int | None = None) -> str:
    """截断工具返回文本，超限追加 TRUNCATED 标记并注明 shown/total。"""
    lim = limit if limit is not None else TOOL_RESULT_LIMIT
    if result is None:
        return ""
    if isinstance(result, str):
        s = result
    else:
        try:
            s = json.dumps(result, ensure_ascii=False)
        except Exception:
            s = str(result)
    if len(s) <= lim:
        return s
    shown = lim
    total = len(s)
    truncated = s[:lim]
    # 追加 TRUNCATED 标记，便于审计与测试识别
    return f"{truncated}\n...[TRUNCATED shown={shown}/total={total}]"


def fit_records(records: list[Any], limit: int | None = None, per_record_limit: int | None = None) -> list[str]:
    """分页打包：在总字符预算内尽可能容纳多条记录，单条可独立限长。"""
    lim = limit if limit is not None else TOOL_RESULT_LIMIT
    out: list[str] = []
    total = 0
    for r in records:
        s = truncate_tool_result(r, limit=per_record_limit if per_record_limit is not None else lim)
        # 单条已超总预算时进一步截断，避免整体溢出
        if total + len(s) > lim and out:
            remaining = lim - total
            if remaining > 100:
                s = truncate_tool_result(s, limit=remaining)
            else:
                break
        if total + len(s) > lim and not out:
            # 首条即超限：直接返回截断后的单条，保证至少有结果
            out.append(truncate_tool_result(s, limit=lim))
            break
        out.append(s)
        total += len(s)
        if total >= lim:
            break
    return out
