"""Golden set v1 评测 runner（W2 · PR-113/114，配套 PR-112 真模型 E2E）。

fake 模式（默认，无需 key）：只验证评测管道与断言机器自洽，**不产出模型质量结论**。
    python evals/run_golden.py --mode fake

real 模式（需要真实 LLM 后端；缺 key/后端不可达时如实 skip，不伪造通过率）：
    python evals/run_golden.py --mode real
    python scripts/eval_real.py            # 等价入口

real 模式的客户端**必须与生产 `/v1/query` 同一套构建路径**（`api/server.py` 的
`LLMFactory -> LLMClient -> get_definitions() -> bind_tools`）：只有把工具 schema
真正绑到模型上，`tool_selection` 才度量「模型会不会选工具」，而不是「模型看不见工具」。
绑定失败时结果里显式写 `provider.tools_bound=false`，绝不假装绑上。

产物：evals/results_golden_{fake,real}.json，含逐用例结果 + 聚合指标
（成功率 / 工具选择 P·R·F1 / grounding P·R·F1 / p50·p95 / 单任务成本）+ usage_ledger。
"""

from __future__ import annotations

import argparse
import dataclasses
import functools
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_GOLDEN = HERE / "golden" / "golden_v1.jsonl"

# 直接 `python evals/run_golden.py` 时把仓库 src 放进 path（已安装包时无副作用）
_SRC = HERE.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

try:  # 包内导入（tests / repo root 在 sys.path 时）
    from evals.metrics import aggregate, failure_class
except ImportError:  # 直接 `python evals/run_golden.py` 时无包上下文
    sys.path.insert(0, str(HERE))
    from metrics import aggregate, failure_class  # type: ignore[no-redef]  # noqa: E402

GOLDEN_VERSION = "v1"

# 与生产 `/v1/query`（server.py:2187 `max_iterations=5`）同一轮次预算（也是
# AgentLoop 的默认值）。旧值 2 是评测侧自造的更紧预算：工具失败/纠正轮一旦需要第 3 轮
# 就会提前 `max_iterations` 收尾，把「预算不够」记成 `not_terminated`，属于口径噪声。
EVAL_MAX_ITERATIONS = 5

CATEGORIES = {
    "market_data",
    "fundamentals",
    "indicator_factor",
    "correlation",
    "backtest",
    "multi_tool",
    "grounding_refusal",
    "authz_denied",
    "zh_terms",
}

REQUIRED_FIELDS = (
    "id",
    "category",
    "query",
    "expected_tools",
    "expected_contains",
    "expected_not_contains",
    "max_latency_ms",
)

# 拒答判定是**关键词启发式**（在报告里显式标注），不冒充语义判定
REFUSAL_MARKERS = (
    "无法",
    "不能",
    "不可",
    "不予",
    "不提供",
    "不具备",
    "不支持",
    "没有权限",
    "无权",
    "拒绝",
    "抱歉",
    "超出",
    "不构成投资建议",
    "风险提示",
    "无法预测",
)

# 空数据降级判定：也必须显式说明「没有数据」，不得编造数值（同样是关键词启发式）
NO_DATA_MARKERS = (
    "no_data",
    "无数据",
    "暂无",
    "不可用",
    "无法获取",
    "没有数据",
    "获取失败",
    "数据源",
    "ok=false",
)


# 与生产 `/v1/query`（server.py:2068-2073）同口径的生成参数；改了这里就等于改了
# 被测对象，报告里必须同步说明，否则两轮数字不可比。
REAL_STREAMING = True
REAL_TEMPERATURE = 0.2


def base_url_host(base_url: str | None) -> str | None:
    """只保留 `host[:port]`，丢弃 userinfo/path/query（base_url 可能内嵌 key）。"""
    raw = str(base_url or "").strip()
    if not raw:
        return None
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:  # 非法 URL 不回显原文（可能含密钥）
        return "<invalid>"
    if not host:
        return None
    return f"{host}:{port}" if port else host


@dataclasses.dataclass
class RealClient:
    """real 模式客户端构建结果（`llm=None` 表示如实 skip，不是一个可用的假客户端）。"""

    llm: Any = None
    provider: dict[str, Any] = dataclasses.field(default_factory=dict)
    skip_reason: str | None = None
    skip_detail: str = ""
    warnings: list[str] = dataclasses.field(default_factory=list)


def build_real_llm(
    settings: Any = None,
    *,
    factory: Any = None,
    definitions: list[dict[str, Any]] | None = None,
) -> RealClient:
    """按生产 `/v1/query` 同序构建客户端并绑工具（评测口径对齐的关键）。

    顺序与 `api/server.py` 保持一致：
    `Settings()` → key → `LLMFactory(settings)` → `model_for_stage("plan")`（失败回退
    `settings.llm_model`）→ `factory.create(..., streaming=True, temperature=0.2)` →
    非 `LLMClient` 包一层 → `get_definitions()` → `_chat.bind_tools` 优先 / `llm.bind_tools` 兜底。

    任何一步失败都不抛：绑定失败记 warning + `provider.tools_bound=false`，评测继续跑，
    这样「没绑上」会体现为工具指标低而不是静默变成「模型不会用工具」。
    """
    from hero_quant.config.settings import Settings
    from hero_quant.llm.client import LLMClient
    from hero_quant.llm.factory import LLMFactory

    s = settings if settings is not None else Settings()
    host = base_url_host(getattr(s, "llm_base_url", None))
    key = getattr(s, "api_key", None) or getattr(s, "openai_api_key", None) or ""
    key = key.strip() if isinstance(key, str) else ""

    if not key:
        return RealClient(
            llm=None,
            provider={
                "chain": [],
                "active": None,
                "model": None,
                "tools_bound": False,
                "tools_available": 0,
                "base_url_host": host,
            },
            skip_reason="no_llm_backend",
            skip_detail="no api_key/openai_api_key configured; real-LLM assertions are not meaningful",
            warnings=["llm_api_key_missing"],
        )

    fac = factory if factory is not None else LLMFactory(s)
    provider_name = str(getattr(fac, "provider", None) or "openai")
    model = str(getattr(s, "llm_model", "") or "")
    warnings: list[str] = []
    try:
        model_info = fac.model_for_stage("plan")
        model = str(getattr(model_info, "name", None) or model)
    except (ValueError, TypeError, AttributeError, RuntimeError) as exc:
        # 与 server.py:2064-2068 同款回退：catalog 外模型不阻断，但必须留痕
        warnings.append(f"model_for_stage_fallback:{type(exc).__name__}")
        model = str(getattr(s, "llm_model", "") or "")

    llm = fac.create(
        model=model,
        api_key=key,
        base_url=getattr(s, "llm_base_url", None),
        streaming=REAL_STREAMING,
        temperature=REAL_TEMPERATURE,
    )
    if not isinstance(llm, LLMClient):
        llm = LLMClient(llm, timeout=30)

    defs: list[dict[str, Any]] = list(definitions) if definitions is not None else []
    if definitions is None:
        from hero_quant.tools.registry import get_definitions

        defs = list(get_definitions())

    tools_bound = False
    if not defs:
        warnings.append("tools_definitions_empty")
    else:
        chat = getattr(llm, "_chat", None)
        if chat is not None and hasattr(chat, "bind_tools"):
            try:
                llm._chat = chat.bind_tools(defs)  # type: ignore[union-attr]
                tools_bound = True
            except Exception as exc:  # noqa: BLE001 - 绑定失败不得中断评测，必须落成 tools_bound=false
                warnings.append(f"bind_chat_failed:{type(exc).__name__}")
        elif hasattr(llm, "bind_tools"):
            try:
                llm = llm.bind_tools(defs)  # type: ignore[attr-defined]
                tools_bound = True
            except Exception as exc:  # noqa: BLE001 - 同上
                warnings.append(f"bind_llm_failed:{type(exc).__name__}")
        else:
            warnings.append("no_bind_tools_support")

    provider = {
        "chain": [provider_name],
        "active": provider_name,
        "model": model,
        "tools_bound": tools_bound,
        "tools_available": len(defs),
        "base_url_host": host,
    }
    if warnings:
        provider["bind_warnings"] = warnings
    return RealClient(llm=llm, provider=provider, warnings=warnings)


class FakeLLM:
    """与 evals/run.py / server.py::_FakeLLM 同模板的确定性假模型（仅管道冒烟）。"""

    def stream_chat(self, goal: str, timeout=None):
        text = (
            f"600519.SH close 1680.2 report metrics sharpe 1.62 "
            f"grounding_verified True for query: {goal}\n"
            "（演示模式：未配置 HERO_API_KEY，数据为合成占位）\n"
            "本结果为合成占位，不构成投资建议\n"
        )
        yield {"type": "text", "text": text}

    def invoke(self, goal: str):
        return self.stream_chat(goal)

    def chat(self, goal: str):
        return self.stream_chat(goal)

    def __call__(self, goal: str):
        return self.stream_chat(goal)


def load_golden(path: Path = DEFAULT_GOLDEN) -> list[dict[str, Any]]:
    """读 JSONL；坏行直接 fail-closed（带行号）。"""
    cases: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            raw = line.strip()
            if not raw:
                continue
            try:
                cases.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno} JSON invalid: {exc}") from exc
    return cases


def validate_golden(cases: list[dict[str, Any]], tool_names: set[str]) -> list[str]:
    """结构校验：字段齐、id 唯一、工具已注册、类别受控、时延预算为正。"""
    errors: list[str] = []
    seen: set[str] = set()
    for idx, case in enumerate(cases, 1):
        cid = str(case.get("id") or f"line{idx}")
        missing = [f for f in REQUIRED_FIELDS if f not in case]
        if missing:
            errors.append(f"{cid}: missing_fields={missing}")
            continue
        if cid in seen:
            errors.append(f"{cid}: duplicate_id")
        seen.add(cid)
        if not str(case.get("query") or "").strip():
            errors.append(f"{cid}: empty_query")
        if case["category"] not in CATEGORIES:
            errors.append(f"{cid}: unknown_category={case['category']!r}")
        for tool in case["expected_tools"]:
            if tool not in tool_names:
                errors.append(f"{cid}: tool_not_registered:{tool}")
        for field in ("expected_contains", "expected_not_contains"):
            if not isinstance(case[field], list):
                errors.append(f"{cid}: {field}_not_list")
        try:
            if int(case["max_latency_ms"]) <= 0:
                errors.append(f"{cid}: max_latency_ms_not_positive")
        except (TypeError, ValueError):
            errors.append(f"{cid}: max_latency_ms_not_int")
        if case.get("expect_grounding") not in (True, False, None):
            errors.append(f"{cid}: expect_grounding_not_bool")
        if case.get("expect_no_data") not in (True, False, None):
            errors.append(f"{cid}: expect_no_data_not_bool")
    return errors


def golden_sha256(path: Path = DEFAULT_GOLDEN) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ToolCallRecorder:
    """记录本轮实际执行过的工具名（在 TOOL_REGISTRY 上套一层，退出即还原）。

    只用于评测观测，不改工具语义；`functools.wraps` 保留 `__wrapped__`，
    AgentLoop 的签名探测（`inspect.signature(spec.func)`）仍看到原签名。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._registry: dict[str, Any] = {}
        self._saved: dict[str, Any] = {}

    def _wrap(self, name: str, fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            self.calls.append(name)
            return fn(*args, **kwargs)

        return wrapper

    def __enter__(self) -> "ToolCallRecorder":
        from hero_quant.tools.registry import TOOL_REGISTRY

        self._registry = TOOL_REGISTRY
        for name, spec in list(TOOL_REGISTRY.items()):
            self._saved[name] = spec.func
            spec.func = self._wrap(name, spec.func)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        for name, fn in self._saved.items():
            spec = self._registry.get(name)
            if spec is not None:
                spec.func = fn
        self._saved.clear()

    def reset(self) -> None:
        self.calls.clear()


def _contains(haystack: str, needle: str) -> bool:
    """大小写不敏感包含（中文/英文术语统一口径）。"""
    return str(needle).lower() in haystack.lower()


def _refusal_detected(text: str) -> bool:
    return any(_contains(text, marker) for marker in REFUSAL_MARKERS)


def _no_data_declared(text: str) -> bool:
    return any(_contains(text, marker) for marker in NO_DATA_MARKERS)


def run_case(
    case: dict[str, Any],
    llm: Any,
    recorder: ToolCallRecorder | None = None,
    capture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """跑一条用例并断言；异常收敛为失败分类，不中断整轮。

    `capture`（可选，real 模式用）回填 `text` / `llm_usage` / `elapsed_ms`：
    usage 归集需要完整文本与 `LoopResult.llm_usage`，而行内 `text_preview` 是截断的。
    fake 模式不传 `capture`，行结构保持原样。
    """
    from hero_quant.agent.loop import AgentLoop

    if recorder is not None:
        recorder.reset()
    failures: list[str] = []
    text = ""
    reason = ""
    terminated = False
    grounding_verified = False
    elapsed_ms = 0.0
    res = None
    t0 = time.monotonic()
    try:
        loop = AgentLoop(llm=llm, max_iterations=EVAL_MAX_ITERATIONS, trace=False)
        res = loop.run(str(case["query"]))
        elapsed_ms = (time.monotonic() - t0) * 1000
        text = res.text or ""
        reason = res.reason or ""
        terminated = bool(res.terminated)
        grounding_verified = bool(res.grounding_verified)
        if not terminated:
            failures.append("not_terminated")
    except Exception as exc:  # noqa: BLE001 - 评测必须把任何异常收敛为失败分类
        elapsed_ms = (time.monotonic() - t0) * 1000
        failures.append(f"loop_exception:{type(exc).__name__}:{exc}")

    for token in case.get("expected_contains") or []:
        if not _contains(text, token):
            failures.append(f"content_missing:{token}")
    for token in case.get("expected_not_contains") or []:
        if _contains(text, token):
            failures.append(f"unexpected_content:{token}")
    if case.get("expect_refusal") and not _refusal_detected(text):
        failures.append("refusal_missing")
    if case.get("expect_no_data") and not _no_data_declared(text):
        failures.append("no_data_not_declared")
    try:
        if elapsed_ms > float(case["max_latency_ms"]):
            failures.append(f"latency_exceeded:{round(elapsed_ms)}ms>{case['max_latency_ms']}ms")
    except (TypeError, ValueError, KeyError):
        failures.append("latency_budget_invalid")

    tools_called = sorted(set(recorder.calls)) if recorder is not None else []
    if capture is not None:
        # 只透出后端计量值（可能为 None）；绝不在此处用估算冒充 usage
        capture["llm_usage"] = getattr(res, "llm_usage", None) if res is not None else None
        capture["text"] = text
        capture["elapsed_ms"] = elapsed_ms
    return {
        "id": case.get("id"),
        "category": case.get("category"),
        "passed": not failures,
        "failures": failures,
        "failure_class": failure_class(failures),
        "elapsed_ms": round(elapsed_ms, 3),
        "reason": reason,
        "text_preview": text[:160],
        "expected_tools": list(case.get("expected_tools") or []),
        "tools_called": tools_called,
        "expect_grounding": case.get("expect_grounding"),
        "grounding_verified": grounding_verified,
    }


def _skipped_payload(reason: str, detail: str, *, golden: dict[str, Any]) -> dict[str, Any]:
    """fail-closed 的跳过产物：只有状态，没有任何「通过率」数字。"""
    return {
        "mode": "real",
        "status": "skipped",
        "reason": reason,
        "detail": detail,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "golden_set": golden,
    }


def _write(payload: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[eval-golden] wrote {out_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="W2 golden set v1 评测")
    parser.add_argument("--mode", choices=["fake", "real"], default="fake")
    parser.add_argument("--golden", default=str(DEFAULT_GOLDEN), help="golden JSONL 路径")
    parser.add_argument("--out", default=None, help="结果 JSON（默认 evals/results_golden_<mode>.json）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（调试用）")
    parser.add_argument("--ids", default=None, help="逗号分隔的用例 id 白名单")
    parser.add_argument("--min-pass-rate", type=float, default=None, help="real 模式回归门（0~1），不传则不阻断")
    parser.add_argument("--expect-count", type=int, default=50, help="golden 条数期望（默认 50）")
    args = parser.parse_args(argv)

    golden_path = Path(args.golden)
    try:
        cases = load_golden(golden_path)
    except (OSError, ValueError) as exc:
        print(f"[eval-golden] FAILED to load golden set: {exc}", file=sys.stderr)
        return 2
    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        cases = [c for c in cases if str(c.get("id")) in wanted]
    if args.limit is not None:
        cases = cases[: max(0, args.limit)]

    from hero_quant.tools import backtest as _bt  # noqa: F401  注册工具
    from hero_quant.tools import correlation as _corr  # noqa: F401
    from hero_quant.tools import market_data as _md  # noqa: F401
    from hero_quant.tools import presentation as _pres  # noqa: F401
    from hero_quant.tools import quantlib_tool as _ql  # noqa: F401
    from hero_quant.tools.registry import TOOL_REGISTRY

    tool_names = set(TOOL_REGISTRY)
    errors = validate_golden(cases, tool_names)
    golden_meta = {
        "path": str(golden_path),
        "version": GOLDEN_VERSION,
        "count": len(cases),
        "sha256": golden_sha256(golden_path),
        "categories": sorted({str(c.get("category")) for c in cases}),
    }
    print(f"[eval-golden] cases={len(cases)} mode={args.mode} sha256={golden_meta['sha256'][:12]}")
    print(f"[eval-golden] registered_tools={len(tool_names)}")
    if errors:
        for err in errors:
            print(f"[eval-golden] GOLDEN INVALID {err}", file=sys.stderr)
        return 2
    if len(cases) == args.expect_count:
        print(f"[eval-golden] golden size ok: {len(cases)}")
    else:
        print(f"[eval-golden] WARN golden size {len(cases)} != expected {args.expect_count}")

    out_path = Path(args.out) if args.out else HERE / f"results_golden_{args.mode}.json"

    if args.mode == "fake":
        with ToolCallRecorder() as recorder:
            rows = [run_case(case, FakeLLM(), recorder) for case in cases]
        payload = {
            "mode": "fake",
            "status": "ok",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "golden_set": golden_meta,
            "refusal_detection": "keyword_heuristic",
            "metrics": aggregate(rows, quality=False),
            "results": rows,
        }
        _write(payload, out_path)
        print("[eval-golden] fake 模式只验证评测管道；模型质量结论必须来自 --mode real")
        return 0

    # ---------------- real 模式 ----------------
    # 口径缺陷修复（上一轮 22/50 的教训）：原实现用 `LLMGateway` 当 llm，而网关没有
    # `bind_tools`，模型收不到工具 schema → `tool_selection` 恒为 0（tp=0/fn=55）。
    # 那是「模型看不见工具」，不是「模型不会选工具」。这里改成与生产 `/v1/query`
    # 完全一致的构建路径（server.py:2050-2095）。
    from hero_quant.config.settings import Settings
    from hero_quant.llm.cost import UsageLedger, build_usage

    client = build_real_llm(Settings())
    provider_meta = dict(client.provider)
    print(
        f"[eval-golden] client model={provider_meta.get('model')} "
        f"tools_bound={provider_meta.get('tools_bound')} "
        f"tools_available={provider_meta.get('tools_available')} "
        f"base_url_host={provider_meta.get('base_url_host')}"
    )
    for warning in client.warnings:
        print(f"[eval-golden] WARN client {warning}", file=sys.stderr)
    if client.llm is None:
        detail = client.skip_detail or "no usable LLM backend"
        print(f"[eval-golden] SKIP real 模式：{detail}")
        _write(
            _skipped_payload(
                client.skip_reason or "no_llm_backend",
                f"{detail}; real-LLM assertions are not meaningful",
                golden=golden_meta,
            ),
            out_path,
        )
        return 0

    provider_name = str(provider_meta.get("active") or "openai")
    model_name = str(provider_meta.get("model") or "")
    ledger = UsageLedger()
    rows: list[dict[str, Any]] = []
    with ToolCallRecorder() as recorder:
        for case in cases:
            capture: dict[str, Any] = {}
            row = run_case(case, client.llm, recorder, capture=capture)
            # 后端 usage 优先；后端没给（LoopResult.llm_usage=None）时 build_usage 会
            # 按字符启发式估算并置 estimated=True —— 如实透出，不用 0 冒充计量。
            usage = build_usage(
                None,
                str(case.get("query") or ""),
                str(capture.get("text") or ""),
                provider=provider_name,
                model=model_name,
                latency_s=round(float(capture.get("elapsed_ms") or 0.0) / 1000.0, 6),
                usage=capture.get("llm_usage"),
            )
            ledger.add(usage)
            row["usage"] = usage.to_dict()
            row["cost_usd"] = usage.cost_usd
            row["input_tokens"] = usage.input_tokens
            row["output_tokens"] = usage.output_tokens
            rows.append(row)

    ledger_summary = ledger.summary()
    metrics = aggregate(rows, quality=True)
    estimated_cases = sum(1 for r in rows if (r.get("usage") or {}).get("estimated"))
    payload = {
        "mode": "real",
        "status": "ok",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "golden_set": golden_meta,
        "provider": provider_meta,
        "cost_accounting": {
            "pricing_source": "hero_quant.llm.catalog.MODEL_CATALOG",
            "unit": "USD_per_1M_tokens",
            "unpriced_policy": "catalog 外模型 cost_usd=null，单列不计入 total",
            "usage_source": "LoopResult.llm_usage（后端计量）优先；后端未给 usage 时按字符启发式估算并置 estimated=true",
            "estimated_cases": estimated_cases,
            "unpriced_cases": ledger_summary.get("unpriced_calls"),
        },
        "usage_ledger": ledger_summary,
        "refusal_detection": "keyword_heuristic",
        "metrics": metrics,
        "results": rows,
    }
    _write(payload, out_path)

    pass_rate = metrics.get("pass_rate")
    qm = metrics.get("quality_metrics") or {}
    print(f"[eval-golden] pass_rate={pass_rate} failure_classes={metrics.get('failure_classes')}")
    print(f"[eval-golden] latency_ms={metrics.get('latency_ms')}")
    print(f"[eval-golden] tool_selection={(qm.get('tool_selection') or {})}")
    print(f"[eval-golden] grounding={(qm.get('grounding') or {})}")
    print(f"[eval-golden] cost={(qm.get('cost') or {})}")
    print(
        f"[eval-golden] usage_ledger calls={ledger_summary.get('calls')} "
        f"tokens={ledger_summary.get('total_tokens')} "
        f"cost_usd={ledger_summary.get('total_cost_usd')} "
        f"priced_calls={ledger_summary.get('priced_calls')} "
        f"unpriced_calls={ledger_summary.get('unpriced_calls')} "
        f"estimated_cases={estimated_cases}"
    )
    if not provider_meta.get("tools_bound"):
        # 不 bound 就等于没测「工具选择」：显式喊出来，别让低分被读成模型能力
        print(
            f"[eval-golden] TOOLS NOT BOUND model={model_name} warnings={provider_meta.get('bind_warnings')}",
            file=sys.stderr,
        )

    if args.min_pass_rate is not None and pass_rate is not None and pass_rate < args.min_pass_rate:
        print(f"[eval-golden] REGRESSION pass_rate {pass_rate} < gate {args.min_pass_rate}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
