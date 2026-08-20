# tests/test_sandbox_base.py
def test_sandbox_ast_and_allowlist(tmp_path):
    from hero_quant.sandbox.ast_guard import check_import_allowlist
    assert check_import_allowlist("import pandas\nimport socket") is False
    assert check_import_allowlist("import pandas\nimport numpy") is True
    from hero_quant.sandbox.policy import resolve_policy
    p = resolve_policy(mode="workspace-write", workspace_root=str(tmp_path))
    assert "workspaceRoot" in p
