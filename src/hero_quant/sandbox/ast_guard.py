"""L0 AST guard — allowlist + banned patterns, deep scan."""
import ast

ALLOWED_ROOTS = {"pandas", "numpy", "scipy", "math", "typing"}

# Explicit bans per spec: socket / subprocess / ctypes / requests / eval / __import__
# os.system is banned via attribute check; os import itself is treated as banned
# when used with dangerous attrs, but also blocked if not in allowlist.
BANNED_IMPORT_ROOTS = {"socket", "subprocess", "ctypes", "requests", "os"}
BANNED_CALL_NAMES = {"eval", "exec", "__import__"}
# attribute bans: (base, attr)
BANNED_ATTRS = {
    ("os", "system"),
    ("os", "popen"),
    ("os", "execve"),
    ("os", "spawnl"),
    ("os", "spawnlp"),
    ("subprocess", "Popen"),
    ("subprocess", "call"),
    ("subprocess", "run"),
    ("subprocess", "check_call"),
    ("subprocess", "check_output"),
}


def _is_banned_attribute(node: ast.Attribute) -> bool:
    """Check if attribute access matches banned list or is on banned root."""
    # direct (os.system) check
    if isinstance(node.value, ast.Name):
        base = node.value.id
        attr = node.attr
        if (base, attr) in BANNED_ATTRS:
            return True
        # any ctypes.* / socket.* / requests.* attribute is banned
        if base in {"ctypes", "socket", "requests"}:
            return True
        if base == "subprocess":
            return True
        if base == "os" and attr in {"system", "popen", "execve", "spawnl", "spawnlp", "execv", "execl"}:
            return True
    return False


def check_import_allowlist(code: str) -> bool:
    """
    Return True if code only uses allowlisted imports and no banned patterns.
    Deep scans nested functions/classes via ast.walk.
    """
    if not code or not code.strip():
        return True
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    for node in ast.walk(tree):
        # Import: import X, import X.Y
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                # banned roots always fail
                if root in BANNED_IMPORT_ROOTS:
                    return False
                if root not in ALLOWED_ROOTS:
                    # allowlist enforcement: any non-allowlisted root is denied
                    return False
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                return False
            root = node.module.split(".")[0]
            if root in BANNED_IMPORT_ROOTS:
                return False
            if root not in ALLOWED_ROOTS:
                return False
        elif isinstance(node, ast.Call):
            # banned builtin calls: eval(...), exec(...), __import__(...)
            func = node.func
            if isinstance(func, ast.Name) and func.id in BANNED_CALL_NAMES:
                return False
            if isinstance(func, ast.Attribute):
                if _is_banned_attribute(func):
                    return False
                # also ban ctypes.*() calls generically
                # _is_banned_attribute already covers; extra guard for nested: e.g., os.system
                # check chained attribute like os.path? os.path not banned, so ignore
                pass
        elif isinstance(node, ast.Attribute):
            # bare attribute access without call (e.g., x = os.system) should also be banned
            if _is_banned_attribute(node):
                return False

    return True


def assert_allowlist(code: str) -> None:
    """Raise ValueError if not allowlisted."""
    if not check_import_allowlist(code):
        raise ValueError("import allowlist violation or banned pattern detected")
