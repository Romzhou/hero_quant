"""OpenAPI 契约快照测试（PR-182）。

为什么存在：`/v1/*` 是对前端与外部消费方的**公开契约**。删一个 path、把某个 query 参数从
可选改必填、删掉一个响应字段、改一个状态码，都不会让任何既有单测变红（它们各自只断言自己
关心的那条路），但会让前端联调当场炸、让已经发出去的 SDK 静默失效。本文件把
`app.openapi()` 固化成快照 `tests/contracts/openapi_snapshot.json`：任何表面形状变化都先在
测试里**可见**，再由人判定是不是破坏性变更。策略与更新流程见 `docs/api-contract.md`。

边界（别误解它的证明力）：快照证明的是"表面契约没偷偷变"，**不证明**接口行为正确——
状态码含义、鉴权、字段语义、错误码仍由各自的行为测试负责。

更新快照（唯一合法方式，禁止手改 JSON）：

    UPDATE_OPENAPI=1 pytest tests/test_openapi_contract.py

更新模式只重写快照并 skip（不静默通过），必须人工 review `git diff` 后再提交。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterator

import pytest

from hero_quant.api.server import app

# 快照落点：tests/contracts/ 是纯数据目录，pytest 不会把它当用例收集。
SNAPSHOT_PATH = Path(__file__).resolve().parent / "contracts" / "openapi_snapshot.json"

UPDATE_ENV = "UPDATE_OPENAPI"

# 规范序列化参数：sort_keys 消除字典序抖动、indent 固定排版、ensure_ascii=False 保留中文描述。
# 三者合起来保证「同一份 schema 两次生成，字节相同」，与 PYTHONHASHSEED / 时区无关。
_CANONICAL_KWARGS: dict[str, Any] = {"ensure_ascii": False, "sort_keys": True, "indent": 2}

_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})

# 差异报告最多打印的行数（防止一次大改把 CI 日志刷爆）。
_MAX_REPORT_LINES = 80

# 关键路径基线。派单（PR-182）原文写的是 `/health`，但本仓没有注册 `/health` 路由：它只出现在
# 鉴权中间件的匿名放行白名单里（src/hero_quant/auth/middleware.py:29-30），`app.routes` 无对应
# handler。真正的存活探针是 `/live`（server.py:1313）、就绪探针是 `/ready`（server.py:1811），
# 与 AGENTS.md §3「/ready 绿」一致。此处按**仓库实际**锁 `/live`，偏差记录在
# docs/api-contract.md「派单与实现的偏差」，不在测试里断言一个不存在的路由。
_REQUIRED_PATHS: tuple[str, ...] = (
    "/v1/query",
    "/v1/query/stream",
    "/v1/backtest/metrics.json",
    "/v1/audit",
    "/live",
    "/ready",
)

# 泄漏扫描：公开 schema 里不许出现示例域名 / 本机名 / 内网网段（信息泄漏 + 联调误导）。
_LEAK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bexample\.(?:com|org|net)\b", "example.com 之类示例域名"),
    (r"\blocalhost\b", "localhost"),
    (r"\b127\.0\.0\.1\b", "回环地址 127.0.0.1"),
    (r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "内网 10.0.0.0/8"),
    (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "内网 192.168.0.0/16"),
    (r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b", "内网 172.16.0.0/12"),
)

# 文档性字段：变化可读但通常不破坏契约，单独标 COSMETIC，避免把 review 注意力带偏。
_DOC_KEYS = frozenset(
    {"description", "summary", "title", "termsOfService", "url", "email", "examples", "tags"}
)

_SEV_BREAKING = "BREAKING"
_SEV_ADDITIVE = "ADDITIVE"
_SEV_COSMETIC = "COSMETIC"

# 空容器摊平后的哨兵值（仅在"节点被清空"时用作叶子，避免整个子树的叶子凭空消失）
_EMPTY_SENTINELS = frozenset({"<空对象>", "<空数组>"})


# ---------------------------------------------------------------------------
# 快照读写（一律经 app.openapi()，不手抄 JSON）
# ---------------------------------------------------------------------------
def _current_spec() -> dict[str, Any]:
    """当前 schema：`app.openapi()` 走一遍 JSON 往返，确保与快照比对处在同一 JSON 语义层。"""
    return json.loads(json.dumps(app.openapi(), ensure_ascii=False))


def _canonical_text(spec: dict[str, Any]) -> str:
    """规范文本（末尾带换行，方便 POSIX 工具与 git diff）。"""
    return json.dumps(spec, **_CANONICAL_KWARGS) + "\n"


def _update_requested() -> bool:
    return os.environ.get(UPDATE_ENV, "").strip() not in {"", "0", "false", "False"}


def _snapshot_rel() -> str:
    """快照的相对路径（仓库内展示用；不在仓库内时回退绝对路径）。"""
    try:
        return str(SNAPSHOT_PATH.relative_to(SNAPSHOT_PATH.parents[2]))
    except (IndexError, ValueError):  # pragma: no cover - 仅在文件被搬到仓库外时触发
        return str(SNAPSHOT_PATH)


def _read_snapshot() -> dict[str, Any]:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _write_snapshot(spec: dict[str, Any]) -> Path:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(_canonical_text(spec), encoding="utf-8")
    return SNAPSHOT_PATH


# ---------------------------------------------------------------------------
# 摊平 + 差异（把 OpenAPI 树折叠成「键路径 -> 叶子」再比对）
# ---------------------------------------------------------------------------
# 键路径用 tuple 而不是点串：`/v1/backtest/metrics.json` 这类 path 自带点号，点串会被切碎，
# 而 tuple 让"某子树整体消失"可以用前缀判断精确表达。
def _flatten(node: Any, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    """把 JSON 树摊平成 {键路径: 标量叶子}。

    数组按语义命名而非下标，避免"顺序调换"被误报成契约变更：
    - 全字符串（如 `required`/`enum`）：`required` 展开成逐项成员，其余按集合语义排序拼接；
    - 带唯一 `name` 的对象数组（参数）：以 `[name=q]` 命名；
    - 带唯一 `url` 的对象数组（servers）：以 `[url=...]` 命名；
    - 其余（`anyOf` 之类的联合体）：保留下标。
    """
    out: dict[tuple[str, ...], Any] = {}
    if isinstance(node, dict):
        if not node:
            out[prefix] = "<空对象>"
        for key in sorted(node):
            out.update(_flatten(node[key], (*prefix, str(key))))
        return out
    if isinstance(node, list):
        if not node:
            out[prefix] = "<空数组>"
        elif all(isinstance(item, str) for item in node):
            if prefix and prefix[-1] == "required":
                # 逐项展开：新增/删除「必填字段」要能指名道姓，不能只报整串变了。
                for item in node:
                    out[(*prefix, item)] = True
            else:
                out[prefix] = "{" + ", ".join(sorted(node)) + "}"
        else:
            named = [item for item in node if isinstance(item, dict) and item.get("name")]
            urled = [item for item in node if isinstance(item, dict) and item.get("url")]
            if len(named) == len(node) and len({item["name"] for item in named}) == len(named):
                for item in node:
                    out.update(_flatten(item, (*prefix, f"[name={item['name']}]")))
            elif len(urled) == len(node) and len({item["url"] for item in urled}) == len(urled):
                for item in node:
                    out.update(_flatten(item, (*prefix, f"[url={item['url']}]")))
            else:
                for idx, item in enumerate(node):
                    out.update(_flatten(item, (*prefix, f"[{idx}]")))
        return out
    out[prefix] = node
    return out


def _node_paths(flat: dict[tuple[str, ...], Any]) -> set[tuple[str, ...]]:
    """flat 里所有"还存在的节点"路径：祖先前缀 + 叶子自身的键。

    必须含叶子键本身：某个 dict 被清空时会摊平成一条 `<空对象>` 叶子，若只看严格前缀，
    就会误判「该节点已整体消失」，把 `responses.200` 折叠成 `responses`，丢掉状态码这一级信息。
    """
    nodes: set[tuple[str, ...]] = set()
    for key in flat:
        for cut in range(len(key) + 1):
            nodes.add(key[:cut])
    return nodes


def _collapse(leaves: set[tuple[str, ...]], other_nodes: set[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """把一堆叶子折叠成"最小整块缺失/新增的子树根"。

    不折叠的话，删一个 query 参数会报成 5 行（name/in/required/schema.type/schema.title），
    review 时噪音盖过信号；折叠后只说一句「参数 run_id 没了」。
    """
    roots: set[tuple[str, ...]] = set()
    for leaf in leaves:
        node = leaf
        while len(node) > 1 and node[:-1] not in other_nodes:
            node = node[:-1]
        roots.add(node)
    return sorted(roots)


def _label(key: tuple[str, ...]) -> str:
    """给差异行一个可读类别（path / method / 参数 / 响应状态码 / 必填字段 / 类型 ...）。"""
    tail = key[-1] if key else ""
    if len(key) == 2 and key[0] == "paths":
        return "path"
    if len(key) == 3 and key[0] == "paths" and key[2] in _HTTP_METHODS:
        return "method"
    if len(key) >= 2 and key[-2] == "required":
        return "必填字段"
    if tail == "required":
        return "必填字段"
    # 先认 responses：同一键路径里既不会有 parameters 又会有 responses，顺序只影响可读性。
    if "responses" in key:
        return "响应状态码"
    if tail.startswith("[name=") and "parameters" in key:
        return "参数"
    if tail == "type" or (len(key) >= 2 and key[-2] == "schema" and tail == "type"):
        return "类型"
    if "parameters" in key:
        return "参数"
    if key and key[0] == "components":
        return "响应模型"
    if tail in _DOC_KEYS:
        return "文档"
    return "其他"


def _severity_removed(key: tuple[str, ...]) -> str:
    return _SEV_COSMETIC if key[-1] in _DOC_KEYS else _SEV_BREAKING


def _severity_added(key: tuple[str, ...], new_flat: dict[tuple[str, ...], Any]) -> str:
    # 新增参数一律当 ADDITIVE，但新增「必填」的参数会打断存量调用方，必须报 BREAKING。
    if new_flat.get((*key, "required")) is True:
        return _SEV_BREAKING
    if key and key[-2:-1] == ("required",):
        return _SEV_BREAKING
    return _SEV_COSMETIC if key[-1] in _DOC_KEYS else _SEV_ADDITIVE


def _severity_changed(key: tuple[str, ...], old: Any, new: Any) -> str:
    tail = key[-1] if key else ""
    if len(key) >= 2 and key[-2] == "required":
        return _SEV_BREAKING  # 响应模型必填性变化 = 字段可能缺失/调用方必须补参
    if tail == "required":
        return _SEV_BREAKING if new is True else _SEV_ADDITIVE
    if tail in _DOC_KEYS:
        return _SEV_COSMETIC
    return _SEV_BREAKING  # 类型/默认值/operationId/结构等，默认按破坏性提示，由人降级


def _fmt(key: tuple[str, ...]) -> str:
    return ".".join(key) if key else "<root>"


def _render_value(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def _build_report(snapshot: dict[str, Any], current: dict[str, Any]) -> str:
    """生成结构化差异文本：分「删除 / 新增 / 值变化」三段，每行带类别 + 严重度 + 键路径。"""
    old_flat = _flatten(snapshot)
    new_flat = _flatten(current)
    old_keys, new_keys = set(old_flat), set(new_flat)
    old_nodes, new_nodes = _node_paths(old_flat), _node_paths(new_flat)

    removed = _collapse(old_keys - new_keys, new_nodes)
    added = _collapse(new_keys - old_keys, old_nodes)

    # 某节点被清空时（如 responses 只剩 422），摊平结果会多出一条 `<空对象>` 叶子；
    # 真正有信息量的是"删掉了哪个具体键"那几行，这类哨兵行在同段里重复报会把噪音盖过信号。
    removed = [k for k in removed if not (old_flat.get(k) in _EMPTY_SENTINELS and k[:-1] in new_nodes)]
    added = [k for k in added if not (new_flat.get(k) in _EMPTY_SENTINELS and k[:-1] in old_nodes)]
    changed = sorted(key for key in old_keys & new_keys if old_flat[key] != new_flat[key])

    sections: list[tuple[str, list[str]]] = []

    removed_rows = [
        f"  [{_severity_removed(key)}] {_label(key):<6} {_fmt(key)}"
        for key in removed
    ]
    sections.append(("快照里有、当前没有（删除 / 破坏性）", removed_rows))

    added_rows = [
        f"  [{_severity_added(key, new_flat)}] {_label(key):<6} {_fmt(key)}"
        for key in added
    ]
    sections.append(("当前新增（快照里没有）", added_rows))

    changed_rows = [
        f"  [{_severity_changed(key, old_flat[key], new_flat[key])}] {_label(key):<6} {_fmt(key)}"
        f" : {_render_value(old_flat[key])} -> {_render_value(new_flat[key])}"
        for key in changed
    ]
    sections.append(("同键值变化", changed_rows))

    severity_count = {sev: 0 for sev in (_SEV_BREAKING, _SEV_ADDITIVE, _SEV_COSMETIC)}
    for rows in (removed_rows, added_rows, changed_rows):
        for row in rows:
            for sev in severity_count:
                if row.startswith(f"  [{sev}]"):
                    severity_count[sev] += 1

    total = sum(len(rows) for _, rows in sections)
    lines = [
        "=" * 78,
        "OpenAPI 契约差异：快照 vs 当前 app.openapi()",
        f"快照文件: {_snapshot_rel()}",
        f"差异 {total} 处：BREAKING {severity_count[_SEV_BREAKING]}"
        f" / ADDITIVE {severity_count[_SEV_ADDITIVE]} / COSMETIC {severity_count[_SEV_COSMETIC]}",
        "=" * 78,
    ]
    printed = 0
    for title, rows in sections:
        if not rows:
            continue
        lines.append(f"--- {title} ---")
        for row in rows:
            if printed >= _MAX_REPORT_LINES:
                lines.append(f"  …（还有 {total - printed} 条，已达 {_MAX_REPORT_LINES} 行上限，省略）")
                printed = total
                break
            lines.append(row)
            printed += 1
        if printed >= total:
            break
    lines.extend(
        [
            "",
            "请判定这些变化是否属于有意变更：",
            "  - 若是破坏性变更：同步前端/SDK，并在 PR 描述里贴出上面这段 diff；",
            "  - 确认无误后重新生成快照（不要手改 JSON）：",
            f"      {UPDATE_ENV}=1 pytest tests/test_openapi_contract.py",
            "  - 策略与破坏性变更定义见 docs/api-contract.md。",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 用例 1：快照一致性（本文件的主门）
# ---------------------------------------------------------------------------
def test_snapshot_matches_current_schema() -> None:
    current = _current_spec()
    if _update_requested():
        _write_snapshot(current)
        pytest.skip(
            f"已重写 {_snapshot_rel()}（{len(current.get('paths', {}))} 个 path）"
            "——请人工 review `git diff` 确认变更后再提交"
        )
    if not SNAPSHOT_PATH.exists():
        pytest.fail(
            f"契约快照缺失：{_snapshot_rel()}\n首次生成（或换分支后重建）：{UPDATE_ENV}=1 pytest {Path(__file__).name}",
            pytrace=False,
        )
    snapshot = _read_snapshot()
    if _canonical_text(snapshot) == _canonical_text(current):
        return
    report = _build_report(snapshot, current)
    print(report)  # print 到 stdout（pytest 失败时同样展示），便于 CI 日志直接读
    pytest.fail(report, pytrace=False)


def test_snapshot_file_is_canonically_formatted() -> None:
    """快照文件本身必须是规范形态（排序键 + 2 空格缩进 + 末尾换行）。

    这条专门防"手工编辑 JSON / 别的工具重排"——手工改出来的快照会和下一轮重新生成打架，
    让 diff 变成纯噪音。
    """
    if _update_requested() or not SNAPSHOT_PATH.exists():
        pytest.skip("更新模式或无快照：跳过格式断言")
    text = SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert text == _canonical_text(json.loads(text)), (
        f"{_snapshot_rel()} 不是规范序列化形态；请用 {UPDATE_ENV}=1 pytest {Path(__file__).name} 重新生成，不要手改"
    )


def test_openapi_generation_is_deterministic() -> None:
    """同一进程内两次独立生成（清掉 fastapi 缓存）必须字节相同。

    防的是 schema 里混入时间戳/UUID/随机序——那类抖动会让快照门每跑一次就红一次。
    """
    first = _canonical_text(_current_spec())
    app.openapi_schema = None  # 清缓存，强制重建而不是读上一轮结果
    try:
        second = _canonical_text(_current_spec())
    finally:
        app.openapi_schema = None  # 不残留本次缓存，后续用例各自重建
    assert first == second, "app.openapi() 两次生成不一致：schema 里混入了非确定性字段（时间/随机/迭代序）"


# ---------------------------------------------------------------------------
# 用例 2：与快照无关的独立不变量
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", _REQUIRED_PATHS)
def test_required_path_present(path: str) -> None:
    spec = _current_spec()
    assert path in spec.get("paths", {}), (
        f"关键路径 {path} 未出现在 OpenAPI 里；现存 path: {sorted(spec.get('paths', {}))}"
    )


def test_operation_ids_unique_and_non_empty() -> None:
    spec = _current_spec()
    problems: list[str] = []
    seen: dict[str, str] = {}
    for path, item in sorted(spec.get("paths", {}).items()):
        for method in sorted(set(item) & _HTTP_METHODS):
            op = item[method]
            op_id = op.get("operationId")
            where = f"{method.upper()} {path}"
            if not op_id:
                problems.append(f"  {where}: operationId 缺失")
            elif op_id in seen:
                problems.append(f"  {where}: operationId {op_id!r} 与 {seen[op_id]} 重复")
            else:
                seen[op_id] = where
    assert not problems, "operationId 契约问题（客户端代码生成依赖它唯一）:\n" + "\n".join(problems)


def test_every_operation_declares_2xx_response() -> None:
    spec = _current_spec()
    problems: list[str] = []
    for path, item in sorted(spec.get("paths", {}).items()):
        for method in sorted(set(item) & _HTTP_METHODS):
            op = item[method]
            codes = [str(code) for code in (op.get("responses") or {})]
            if not any(code.startswith("2") for code in codes):
                problems.append(f"  {method.upper()} {path}: 未声明任何 2xx 响应（现声明 {codes or '无'}）")
    assert not problems, "响应声明缺失:\n" + "\n".join(problems)


def _iter_doc_strings(node: Any, prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, str]]:
    """收集「文档性」字符串：servers 全域 + description/summary/title 等字段值。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if prefix == ("servers",) or (prefix and prefix[0] == "servers"):
                if isinstance(value, str):
                    yield ".".join((*prefix, str(key))), value
            elif str(key) in _DOC_KEYS and isinstance(value, str):
                yield ".".join((*prefix, str(key))), value
            else:
                yield from _iter_doc_strings(value, (*prefix, str(key)))
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            yield from _iter_doc_strings(item, (*prefix, f"[{idx}]"))


def test_no_private_host_or_example_domain_leaks() -> None:
    """servers 与描述里不许出现示例域名 / localhost / 内网地址（信息泄漏 + 联调误导）。"""
    spec = _current_spec()
    hits: list[str] = []
    for where, text in _iter_doc_strings(spec):
        for pattern, desc in _LEAK_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                hits.append(f"  {where}: 命中 {desc} -> {text[:120]!r}")
    assert not hits, "OpenAPI 文档里出现疑似泄漏/误导地址:\n" + "\n".join(hits)
