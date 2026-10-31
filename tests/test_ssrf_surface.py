"""SSRF 面收敛回归锁（PR-152 配套）——**边界声明，不是 SSRF 全防护**。

本仓当前**没有用户可控 URL 入口**：工具注册表里没有任何工具接受 `url`/`uri`/
`endpoint`/`webhook` 类参数，出网请求只发生在代码内写死主机的 loader，LLM 网关
地址由 `Settings.llm_base_url` 统一校验（仅 http(s)、禁 userinfo）。

这个文件把上述事实锁成回归测试，目的是让**未来新增的 URL 入口必须先失败**：
一旦有人在工具里加 `url` 参数，本测试即红，强制其同时补 allowlist（域名白名单）
+ 私网/环回/link-local 拦截（可复用 `hero_quant/telemetry/otel.py::_is_allowed_endpoint`
与 `tests/test_telemetry_ssrf.py` 的口径），而不是直接把用户字符串丢给 urllib/httpx。

因此：新增任何用户可控 URL 入口 → 必须先改本文件的边界声明（附 allowlist/私网拦截证据），
不允许把断言删掉或加 skip。
"""

from __future__ import annotations

import importlib
import inspect
import re
import warnings
from typing import Any, Dict, Iterable, Iterator, List, Tuple

import pytest

from hero_quant.config.settings import Settings

# 会直接构成 SSRF 入口的参数名（精确命中）
_FORBIDDEN_EXACT = frozenset({"url", "uri", "endpoint", "webhook"})

# 后缀命中：`base_url` / `callback_uri` / `notify_endpoint` / `hooks_webhook` 同样危险
_FORBIDDEN_SUFFIXES = ("url", "uri", "endpoint", "webhook")

# 冷启动时工具模块未导入 → TOOL_REGISTRY 为空，测试会假绿。核心模块必须导入成功；
# 只读期权工具挂在可选 extra 上，导入失败仅告警（它们的参数面没有 URL 语义）。
_CORE_TOOL_MODULES = (
    "hero_quant.tools.market_data",
    "hero_quant.tools.backtest",
    "hero_quant.tools.quantlib_tool",
    "hero_quant.tools.correlation",
)
_OPTIONAL_TOOL_MODULES = ("hero_quant.mcp.server",)


def _bootstrap_tools() -> List[str]:
    """填充 TOOL_REGISTRY，返回导入失败的模块名（核心模块失败直接抛出）。"""

    for mod in _CORE_TOOL_MODULES:
        importlib.import_module(mod)
    skipped: List[str] = []
    for mod in _OPTIONAL_TOOL_MODULES:
        try:
            importlib.import_module(mod)
        except Exception as exc:  # pragma: no cover - 本机可选依赖齐全
            skipped.append(mod)
            warnings.warn(f"optional tool module unavailable, URL-surface scan limited: {mod}: {exc}", stacklevel=2)
    return skipped


def _iter_schema_params(schema: Any, prefix: str = "") -> Iterator[Tuple[str, str]]:
    """递归遍历 JSON Schema，产出 (路径, 参数名)：嵌套 object 与 array items 都算入口。"""

    if not isinstance(schema, dict):
        return
    props = schema.get("properties")
    if isinstance(props, dict):
        for name, sub in props.items():
            path = f"{prefix}.{name}" if prefix else str(name)
            yield path, str(name)
            yield from _iter_schema_params(sub, path)
    items = schema.get("items")
    if isinstance(items, dict):
        yield from _iter_schema_params(items, f"{prefix}[]")


def _url_like_offenders(definitions: Iterable[Dict[str, Any]]) -> List[str]:
    """返回所有疑似 URL 入口的 `工具名.参数路径`；空列表 = 无用户可控 URL 入口。"""

    offenders: List[str] = []
    for definition in definitions:
        fn = (definition or {}).get("function") or {}
        tool_name = str(fn.get("name") or "<unnamed>")
        for path, param in _iter_schema_params(fn.get("parameters")):
            lowered = param.strip().lower()
            if lowered in _FORBIDDEN_EXACT or lowered.endswith(_FORBIDDEN_SUFFIXES):
                offenders.append(f"{tool_name}.{path}")
    return offenders


# ------------------------------------------------------------------ 工具参数面


def test_no_tool_accepts_url_like_parameter():
    """工具注册表里不得出现 url/uri/endpoint/webhook 类参数（含 base_url 等后缀变体）。"""
    from hero_quant.tools.registry import get_definitions

    skipped = _bootstrap_tools()
    definitions = get_definitions()
    # 防空转：注册表必须真的非空，否则断言是无意义的绿（skipped 说明可选模块覆盖范围）
    assert len(definitions) >= 18, (
        f"tool registry not bootstrapped: {len(definitions)} definitions (optional tool modules skipped: {skipped})"
    )
    tool_names = {d["function"]["name"] for d in definitions}
    assert {"get_market_data", "run_backtest"} <= tool_names

    offenders = _url_like_offenders(definitions)
    assert offenders == [], (
        "新增了用户可控 URL 入口：必须先实现 allowlist + 私网/环回拦截再放行（见本文件 docstring）："
        + ", ".join(offenders)
    )


def test_url_like_detector_is_not_vacuous():
    """自检：检测器对伪造的 url 参数必须报错，否则上面的绿是无意义的。"""
    fake = [
        {
            "type": "function",
            "function": {
                "name": "fake_fetch_tool",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "url": {"type": "string"},
                        "nested": {
                            "type": "object",
                            "properties": {"callback_uri": {"type": "string"}},
                        },
                        "rows": {"type": "array", "items": {"type": "object", "properties": {"webhook": {"type": "string"}}}},
                    },
                },
            },
        }
    ]
    offenders = _url_like_offenders(fake)
    assert offenders == [
        "fake_fetch_tool.url",
        "fake_fetch_tool.nested.callback_uri",
        "fake_fetch_tool.rows[].webhook",
    ]
    # 正常参数（symbol/start/end）不该被误报
    assert _url_like_offenders([{"function": {"name": "ok", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}}}}]) == []


# ------------------------------------------------------------------ 出网 URL 只来自代码常量


def test_tencent_loader_outbound_url_is_code_literal():
    """tencent loader 的出网主机是源码常量，且只有一处 urllib 调用（读源码即可，不联网）。"""
    from hero_quant.data.loaders import tencent as tencent_mod

    src = inspect.getsource(tencent_mod)
    # 1) 唯一出网调用点：urlopen(url, timeout=...)，url 由下面的字面量拼出
    assert src.count("urllib.request.urlopen(") == 1
    assert re.search(r"urllib\.request\.urlopen\(url,\s*timeout=", src)
    # 2) https + 主机是字面量，不允许 f"http://..." 或从 env/参数拼主机
    assert 'f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=' in src
    assert "http://web.ifzq.gtimg.cn" not in src  # 无降级到明文 http 的后门
    # 3) 主机段不来自环境变量/调用方：host 前后没有 .format/插值入口
    assert "web.ifzq.gtimg.cn" in src
    assert "HERO_TENCENT_HOST" not in src and "TENCENT_BASE_URL" not in src
    # 4) 出网方法的入参面只有 symbol/start/end/interval，无 url 形参
    for method in (tencent_mod.TencentLoader.get_bars, tencent_mod.TencentLoader._fetch_live_bars):
        params = set(inspect.signature(method).parameters) - {"self"}
        assert params == {"symbol", "start", "end", "interval"}, f"{method.__name__} gained params: {params}"
        lowered = {p.lower() for p in params}
        assert not lowered & _FORBIDDEN_EXACT
    # 5) 拼进 URL 的 symbol 必须先 quote（防查询串注入/路径穿越）
    assert re.search(r"urllib\.parse\.quote\(tencent_symbol", src)


# ------------------------------------------------------------------ 网关地址校验不回退


@pytest.mark.parametrize("raw,expected", [
    ("http://localhost:3001/v1", "http://localhost:3001/v1"),
    ("https://gateway.example.com/openai/v1/", "https://gateway.example.com/openai/v1"),
])
def test_llm_base_url_accepts_http_and_https(monkeypatch, raw, expected):
    monkeypatch.setenv("HERO_LLM_BASE_URL", raw)
    assert Settings().llm_base_url == expected


@pytest.mark.parametrize("raw", [
    "ftp://gateway.example.com/v1",          # 非 http(s)
    "file:///etc/passwd",                    # 本地文件
    "gopher://gateway.example.com/v1",       # 冷门协议（SSRF 常用）
    "javascript:alert(1)",                   # 伪协议
    "gateway.example.com/v1",                # 缺 scheme
    "http://user:pass@gateway.example.com/v1",  # userinfo（凭据投递）
    "http://",                               # 缺 host
])
def test_llm_base_url_rejects_unsafe_values(monkeypatch, raw):
    """复用 Settings 既有行为做防回退：非 http(s) / userinfo / 缺 host 一律构造即失败。"""
    monkeypatch.setenv("HERO_LLM_BASE_URL", raw)
    with pytest.raises(ValueError):
        Settings()
