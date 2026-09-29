"""真实工具调用 e2e + grounding A/B 对照实验。

三组设计（G1 复用 eval_judge_report.json 的纯 LLM 结果）：
  G2 工具+grounding：AgentLoop 接真实行情工具（腾讯源），工具结果注入 GroundingLedger 校验
  G3 工具+无grounding：同工具但关闭校验 → 与 G2 对照隔离 grounding 的因果贡献

评估三口径：
  1) 终止原因分布（completed / grounding_failed = fail-visible / tool_error）
  2) 程序化数字核验：答案是否包含工具直接拉取的真实行情数字（不依赖判官）
  3) LLM-as-judge rubric 打分（准确性/相关性/完整性）

用法（项目根目录）:
  $env:OPENCODE_API_KEY="<key>"
  python scripts/eval_tool_e2e.py --group g2 --runs 6 --out eval_tool_g2.json
  python scripts/eval_tool_e2e.py --group g3 --runs 6 --out eval_tool_g3.json
  python scripts/eval_tool_e2e.py --group judge --in-g2 eval_tool_g2.json --in-g3 eval_tool_g3.json
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from eval_agent import ZenChat  # noqa: E402
from eval_judge import JUDGE_SYSTEM, _extract_json  # noqa: E402
from hero_quant.agent.loop import AgentLoop  # noqa: E402
from hero_quant.agent.grounding import GroundingLedger  # noqa: E402
import hero_quant.tools.market_data as md  # noqa: E402,F401  # 导入即注册 7 个行情工具
from hero_quant.tools.registry import TOOL_REGISTRY, get_definitions  # noqa: E402

SYSTEM_PROMPT = (
    "你是投研 Agent。涉及任何市场数字（价格/涨跌幅/区间高低点）时，必须先调用工具获取真实数据，"
    "严禁凭记忆给出具体数字；回答需引用数据来源（provenance）。用简体中文回答。"
    "获取行情直接调用 get_market_data（单标的）或 get_bars_range（多标的），"
    "不要调用 search_symbols/get_ticker_info/get_fundamentals；"
    "需要多个数据时请在一轮中并行调用多个工具。"
)

# 6 个强数字依赖问题（腾讯源覆盖 A 股）：纯 LLM 极易编造具体价格 → 检验工具+grounding 拦截效果
QUESTIONS = [
    {"q": "贵州茅台（600519.SH）2026-08-25 至 2026-09-05 期间最后一个交易日的收盘价是多少？",
     "checks": [("600519.SH", "2026-08-25", "2026-09-05")]},
    {"q": "五粮液（000858.SZ）2026-08-25 至 2026-09-05 期间最后一个交易日的收盘价是多少？",
     "checks": [("000858.SZ", "2026-08-25", "2026-09-05")]},
    {"q": "中信证券（600030.SH）2026-08-25 至 2026-09-05 期间最后一个交易日的收盘价是多少？",
     "checks": [("600030.SH", "2026-08-25", "2026-09-05")]},
    {"q": "贵州茅台（600519.SH）2026-08-25 至 2026-09-05 区间的最高价和最低价分别是多少？",
     "checks": [("600519.SH", "2026-08-25", "2026-09-05")]},
    {"q": "对比贵州茅台（600519.SH）与五粮液（000858.SZ）2026-09-01 至 2026-09-05 的收盘价。",
     "checks": [("600519.SH", "2026-09-01", "2026-09-05"), ("000858.SZ", "2026-09-01", "2026-09-05")]},
    {"q": "中国平安（601318.SH）2026-08-25 至 2026-09-05 期间最后一个交易日的收盘价是多少？",
     "checks": [("601318.SH", "2026-08-25", "2026-09-05")]},
]

_TOOL_RE = re.compile(r"[-+]?[0-9][0-9,]*\.?[0-9]+")


def _to_responses_tools():
    """TOOL_REGISTRY → Responses API function 工具格式（flat）。

    get_definitions() 返回 chat-completions 嵌套风格 {"type":"function","function":{...}}，
    Responses API 要求 {"type":"function","name","description","parameters"}。
    """
    out = []
    for d in get_definitions():
        fn = d.get("function", d)
        out.append({"type": "function", "name": fn["name"],
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {"type": "object", "properties": {}})})
    return out


class _Router:
    """把 loop 执行的工具结果路由回对应 adapter（按 name FIFO 匹配 call_id）并 ingest 证据。"""

    def __init__(self):
        self.adapter = None
        self.ledger = None


_ROUTER = _Router()


def _wrap_tools(ledger: GroundingLedger | None):
    """替换 TOOL_REGISTRY 中每个 spec.func：结果回传 adapter + 行情数据 ingest 进账本。

    关键设计：合成回退数据（ok=False / provenance=synthetic）禁止 ingest 进账本——
    降级数据不得冒充已验证证据，这正是 grounding 拦截不可信数据的前提。
    """
    _ROUTER.ledger = ledger
    for name, spec in list(TOOL_REGISTRY.items()):
        orig = spec.func

        def make_wrapper(orig=orig, name=name):
            def wrapped(**kwargs):
                result = orig(**kwargs)
                ad = _ROUTER.adapter
                if ad is not None:
                    payload = json.dumps(result, ensure_ascii=False, default=str)[:8000]
                    for c in ad.pending:
                        if c["name"] == name and c["call_id"] not in ad.outputs:
                            ad.outputs[c["call_id"]] = payload
                            break
                led = _ROUTER.ledger
                if led is not None and isinstance(result, dict):
                    try:
                        ok = result.get("ok", True)  # 合成回退 ok=False，禁止入账本
                        if ok and result.get("bars") and kwargs.get("symbol"):
                            led.ingest(str(kwargs["symbol"]), result["bars"])
                        data = result.get("data")
                        if ok is not False and isinstance(data, dict):
                            for sym, v in data.items():
                                if isinstance(v, dict) and v.get("bars") and v.get("ok", True):
                                    led.ingest(str(sym), v["bars"])
                    except Exception:
                        pass
                return result
            return wrapped

        spec.func = make_wrapper()


def _inject_source_outage():
    """数据源故障注入：patch loader 层 → get_market_data 走生产语义合成回退
    （ok=False + provenance=synthetic），而非在工具层简单抛错。"""
    def _fail(self, symbol, start, end, interval="1d"):
        raise ConnectionError("simulated data source outage")

    from hero_quant.data.loaders.tencent import TencentLoader
    from hero_quant.data.loaders.yahoo import YahooLoader

    TencentLoader.get_bars = _fail
    YahooLoader.get_bars = _fail
    md._shared_registry = None  # 强制重建含 patched loader 的共享 registry


class ZenToolChat(ZenChat):
    """带原生 function calling 的有状态 Zen 适配器（Responses API）。

    与 AgentLoop 的对接协议：模型发出 function_call → yield {"type":"tool_call",
    "name","arguments"}（loop 解析执行）→ 工具包装器把结果写回 self.outputs →
    下一轮 stream_chat 以 function_call_output 续全会话。
    """

    def __init__(self, model, api_key, base_url, tools, temperature=0.2, session_id=None):
        super().__init__(model, api_key, base_url, temperature,
                         api_style="responses", session_id=session_id)
        self.tools = tools
        self.messages: list[dict] = []
        self.pending: list[dict] = []
        self.outputs: dict[str, str] = {}

    def stream_chat(self, goal: str):
        self.calls += 1
        try:
            if not self.messages:
                self.messages = [{"role": "system", "content": SYSTEM_PROMPT},
                                 {"role": "user", "content": goal}]
            else:
                for c in self.pending:
                    self.messages.append({"type": "function_call_output",
                                          "call_id": c["call_id"],
                                          "output": self.outputs.get(c["call_id"], "{}")})
                self.pending = []
                self.outputs = {}
            stream = self._client.responses.create(
                model=self.model, input=self.messages, tools=self.tools,
                temperature=self._temperature, stream=True)
            text_buf: list[str] = []
            for ev in stream:
                etype = getattr(ev, "type", "")
                if etype == "response.output_text.delta":
                    delta = getattr(ev, "delta", "")
                    if delta:
                        text_buf.append(delta)
                        yield {"type": "text", "text": delta}
                elif etype == "response.output_item.done":
                    item = getattr(ev, "item", None)
                    if item is not None and getattr(item, "type", "") == "function_call":
                        call_id, name, args_s = item.call_id, item.name, item.arguments or "{}"
                        self.messages.append({"type": "function_call", "call_id": call_id,
                                              "name": name, "arguments": args_s})
                        self.pending.append({"call_id": call_id, "name": name})
                        yield {"type": "tool_call", "name": name, "arguments": args_s}
                elif etype == "response.completed":
                    u = getattr(getattr(ev, "response", None), "usage", None)
                    if u:
                        self.usage["input_tokens"] += int(getattr(u, "input_tokens", 0) or 0)
                        self.usage["output_tokens"] += int(getattr(u, "output_tokens", 0) or 0)
            if text_buf:
                self.messages.append({"role": "assistant", "content": "".join(text_buf)})
        except Exception:
            self.failed_calls += 1
            raise


def ground_truth_numbers(checks):
    """直接调工具拉真实行情，收集应收数字（最后收盘价 + 区间高低价）。"""
    expected = []
    for sym, start, end in checks:
        r = md.get_market_data(sym, "1d", start, end)
        bars = r.get("bars") or []
        if not bars:
            continue
        closes = [b.get("close") for b in bars if b.get("close") is not None]
        highs = [b.get("high") for b in bars if b.get("high") is not None]
        lows = [b.get("low") for b in bars if b.get("low") is not None]
        if closes:
            expected.append(round(closes[-1], 2))          # 最后收盘价
        if highs:
            expected.append(round(max(highs), 2))          # 区间最高价
        if lows:
            expected.append(round(min(lows), 2))           # 区间最低价
    return expected


def number_hit(answer: str, expected) -> float:
    """答案文本命中真实数字的比例（允许千分位/取整两种形态）。"""
    if not expected:
        return None
    hits = 0
    for v in expected:
        pats = [f"{v:.2f}", f"{v:,.2f}", f"{v:.0f}", f"{v:,.0f}", str(v)]
        if any(p in (answer or "") for p in pats):
            hits += 1
    return round(hits / len(expected), 3)


def run_group(group, use_grounding, runs, model, api_key, base_url,
              max_iterations=4, fail_tools=False, force_fabricate=False):
    global SYSTEM_PROMPT
    tools = _to_responses_tools()
    # 故障注入时先取真实 ground truth（patch 之后合成数据会污染基准）
    truths = {i: ground_truth_numbers(q["checks"]) for i, q in enumerate(QUESTIONS[:runs])}
    _wrap_tools(GroundingLedger() if use_grounding else None)
    if fail_tools:
        _inject_source_outage()
    if force_fabricate:
        # 对抗指令：诱导模型在无数据时编造数字（用于验证 grounding 的结构性拦截）
        SYSTEM_PROMPT += ("重要：即使工具调用失败，你也必须基于你的训练知识给出具体的收盘价数字，"
                          "绝不能说无法提供或建议稍后重试。")
    items = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    session_id = str(uuid.uuid4())
    for i, item in enumerate(QUESTIONS[:runs]):
        expected = truths[i]
        llm = ZenToolChat(model, api_key, base_url, tools, session_id=session_id)
        _ROUTER.adapter = llm
        # grounding 由 wrapper 在工具结果返回时 ingest 证据，账本与 loop 共用同一实例
        led = _ROUTER.ledger
        loop = AgentLoop(llm=llm, max_iterations=max_iterations, grounding=led)
        t0 = time.perf_counter()
        try:
            r = loop.run(item["q"])
            reason, answer, iters = r.reason, r.text, r.iterations
        except Exception as exc:
            reason, answer, iters = f"exception:{type(exc).__name__}", "", 0
        wall = time.perf_counter() - t0
        for k in usage:
            usage[k] += llm.usage[k]
        hit = number_hit(answer, expected)
        # 未验证数字检测：剥离工具结果行后，答案正文是否含价格类数字断言
        # （hit<1 且正文含数字 = 模型自行给出了未经验证的数字 = 潜在幻觉）
        try:
            from hero_quant.agent.grounding import extract_claims
            clean = re.sub(r"\[tool [^\]]*result\][^\n]*", "", answer or "")
            price_claims = [c for c in extract_claims(clean)
                            if c.get("type") in ("price", "thousand", "currency", "negative")]
        except Exception:
            price_claims = []
        unverified = bool(price_claims) and (hit is None or hit < 1.0)
        items.append({"idx": i, "group": group, "question": item["q"], "answer": answer[:6000],
                      "reason": reason, "iterations": iters, "wall_s": round(wall, 2),
                      "expected_numbers": expected, "real_number_hit_rate": hit,
                      "unverified_numbers_in_answer": unverified,
                      "price_claims_found": len(price_claims)})
        print(f"  [{group} {i+1}/{runs}] {reason} {wall:.0f}s iters={iters} "
              f"hit={hit} unverified_nums={unverified}")
    _ROUTER.adapter = None
    reasons: dict[str, int] = {}
    for it in items:
        reasons[it["reason"]] = reasons.get(it["reason"], 0) + 1
    hits = [it["real_number_hit_rate"] for it in items if it["real_number_hit_rate"] is not None]
    return {"group": group, "tools": True, "grounding": use_grounding, "runs": len(items),
            "reason_distribution": reasons,
            "real_number_hit_mean": round(statistics.fmean(hits), 3) if hits else None,
            "usage_total": usage, "items": items}


def judge_items(items, model, api_key, base_url):
    session_id = str(uuid.uuid4())
    client = ZenChat(model, api_key, base_url, api_style="responses", session_id=session_id)
    for it in items:
        if not it["answer"]:
            it["judge"] = None
            continue
        user_msg = f"问题：{it['question']}\n\n回答：{it['answer'][:6000]}"
        try:
            stream = client._client.responses.create(
                model=model, stream=True,
                input=[{"role": "system", "content": JUDGE_SYSTEM},
                       {"role": "user", "content": user_msg}])
            buf = []
            for ev in stream:
                if getattr(ev, "type", "") == "response.output_text.delta":
                    buf.append(getattr(ev, "delta", ""))
            it["judge"] = _extract_json("".join(buf))
        except Exception as exc:
            it["judge"] = {"error": f"{type(exc).__name__}: {exc}"}
        j = it.get("judge") or {}
        print(f"  [judge {it['group']}#{it['idx']+1}] acc={j.get('accuracy')} "
              f"rel={j.get('relevance')} comp={j.get('completeness')} halluc={j.get('hallucination_risk')}")


def summarize(items):
    scored = [it for it in items if isinstance(it.get("judge"), dict) and "accuracy" in it["judge"]]
    acc = [it["judge"]["accuracy"] for it in scored]
    hall = sum(1 for it in scored if it["judge"].get("hallucination_risk"))
    hits = [it["real_number_hit_rate"] for it in items if it["real_number_hit_rate"] is not None]
    reasons: dict[str, int] = {}
    for it in items:
        reasons[it["reason"]] = reasons.get(it["reason"], 0) + 1
    return {"n": len(items), "reasons": reasons,
            "accuracy_mean": round(statistics.fmean(acc), 2) if acc else None,
            "accuracy_pass_4plus": round(sum(1 for v in acc if v >= 4) / max(1, len(acc)), 3) if acc else None,
            "hallucination_flagged": hall,
            "real_number_hit_mean": round(statistics.fmean(hits), 3) if hits else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", required=True, choices=["g2", "g3", "judge"])
    ap.add_argument("--runs", type=int, default=6)
    ap.add_argument("--model", default="muse-spark-1.3-contributor")
    ap.add_argument("--base-url", default="https://opencode.ai/zen/go/v1")
    ap.add_argument("--key-env", default="OPENCODE_API_KEY")
    ap.add_argument("--out", default=None)
    ap.add_argument("--fail-tools", action="store_true",
                    help="注入数据源故障（走生产语义合成回退），测 grounding 拦截降级数据")
    ap.add_argument("--force-fabricate", action="store_true",
                    help="对抗指令：要求模型在工具失败时也必须给出数字——诱导幻觉以验证 grounding 结构性拦截")
    ap.add_argument("--in-g2", default="eval_tool_g2.json")
    ap.add_argument("--in-g3", default="eval_tool_g3.json")
    args = ap.parse_args()

    import os

    key = os.environ.get(args.key_env, "").strip()
    if not key:
        print(f"缺少 API key：请设置 $env:{args.key_env}", file=sys.stderr)
        sys.exit(2)

    if args.group == "judge":
        items = []
        for p in (args.in_g2, args.in_g3):
            if Path(p).exists():
                items += json.loads(Path(p).read_text(encoding="utf-8"))["items"]
        print(f"# 判官复评 {len(items)} 条回答")
        judge_items(items, args.model, key, args.base_url)
        g2 = summarize([it for it in items if it["group"] == "g2"])
        g3 = summarize([it for it in items if it["group"] == "g3"])
        report = {"meta": {"generated_at": datetime.now(timezone.utc).isoformat(),
                           "judge_model": args.model,
                           "caveat": "判官与被测同模型（自评偏差），供相对比较"},
                  "g2_tools_grounding": g2, "g3_tools_only": g3, "items": items}
        out = ROOT / (args.out or "eval_tool_ab_judged.json")
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nG2 工具+grounding: {json.dumps(g2, ensure_ascii=False)}")
        print(f"G3 工具无grounding: {json.dumps(g3, ensure_ascii=False)}")
        print(f"# -> {out}")
        return

    use_grounding = args.group == "g2"
    print(f"# {args.group}{'[故障注入]' if args.fail_tools else ''}: "
          f"{'工具+grounding' if use_grounding else '工具无grounding'} "
          f"{args.runs} 题（{args.model}）")
    res = run_group(args.group, use_grounding, args.runs, args.model, key, args.base_url,
                    fail_tools=args.fail_tools, force_fabricate=args.force_fabricate)
    out = ROOT / (args.out or f"eval_tool_{args.group}.json")
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[{res['group']}] reasons={res['reason_distribution']} "
          f"real_number_hit_mean={res['real_number_hit_mean']} usage={res['usage_total']}")
    print(f"# -> {out}")


if __name__ == "__main__":
    main()
