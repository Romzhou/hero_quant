"""批次18 P2-2：全仓禁止两个模块定义同名类（AGENTS §11 红线）。

按类名严格扫描（不比对整行声明，避免基类不同漏判），重复即失败。
"""

from __future__ import annotations

import re
from pathlib import Path

_CLASS_DEF = re.compile(r"^class ([A-Za-z_][A-Za-z0-9_]*)\b")


def test_no_duplicate_class_definitions_in_src():
    occurrences: dict[str, list[str]] = {}
    for path in sorted(Path("src").rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = _CLASS_DEF.match(line)
            if match:
                occurrences.setdefault(match.group(1), []).append(f"{path}:{lineno}")
    duplicates = {name: places for name, places in occurrences.items() if len(places) > 1}
    assert not duplicates, f"两个模块定义同名类（静默接错风险）: {duplicates}"


def test_loader_validation_error_is_single_shared_definition():
    """DataValidationError 只能有一个定义，四个 loader 复用同一类型。"""
    from hero_quant.data.loaders.akshare_loader import DataValidationError as A
    from hero_quant.data.loaders.ccxt_loader import DataValidationError as C
    from hero_quant.data.loaders.tencent import DataValidationError as T
    from hero_quant.data.loaders.yahoo import DataValidationError as Y

    assert A is C is T is Y
    assert issubclass(T, ValueError)


def test_approval_and_run_not_found_are_distinct_symbols():
    """security/interaction 的审批服务、runs/session 的 RunNotFound 不得同名。"""
    import hero_quant.interaction.approval as interaction_approval
    import hero_quant.security.approval as security_approval
    import hero_quant.runs.store as runs_store
    import hero_quant.session.store as session_store

    assert interaction_approval.InteractionApprovalService is not security_approval.ApprovalService
    assert issubclass(interaction_approval.InteractionApprovalService, security_approval.ApprovalService)
    assert session_store.SessionRunNotFound is not runs_store.RunNotFound
    assert issubclass(session_store.SessionRunNotFound, Exception)
