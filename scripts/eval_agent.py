"""Agent 自动评测管线 — 场景矩阵 + 指标聚合。

产出 eval_report.json：成功率/终止原因分布/轮次/耗时 P50·P95/token/成本/重试率/幻觉拦截率。
mock 模式：脚本化 FakeLLM 注入故障场景，全程确定性可复现，不消耗真实 LLM 配额。

用法（项目根目录）: python scripts/eval_agent.py [--runs 20] [--out eval_report.json]
"""
from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hero_quant.agent.loop import AgentLoop, LoopResult  # noqa: E402
from hero_quant.agent.policies import BudgetBreaker, RetryPolicy  # noqa: E402
from hero_quant.agent.grounding import GroundingLedger  # noqa: E402

# 与 BudgetBreaker 默认定价一致（per 1M tokens），可通过 HERO_LLM_PRICE_IN/OUT 覆盖
PRICE_IN, PRICE_OUT = 0.15, 0.60


class FakeLLM:
    """脚本化 LLM：按剧本 yield chunks；前 fail_times 次调用抛瞬时故障；自记 token 用量。"""

    def __init__(self, chunks, fail_forever=False, fail_times=0):
        self.chunks = list(chunks)
        self.fail_forever = fail_forever
        self.fail_times = fail_times
        self.calls = 0
        self.failed_calls = 0
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    def stream_chat(self, goal):
        self.calls += 1
        if self.fail_forever or self.calls <= self.fail_times:
            self.failed_calls += 1
            raise ConnectionError(f"simulated llm failure (call #{self.calls})")
        for chunk in self.chunks:
            um = chunk.get("usage_metadata") if isinstance(chunk, dict) else None
            if isinstance(um, dict):
                self.usage["input_tokens"] += int(um.get("input_tokens") or 0)
                self.usage["output_tokens"] += int(um.get("output_tokens") or 0)
            yield chunk


def cost_usd(usage: dict) -> float:
    return usage["input_tokens"] / 1e6 * PRICE_IN + usage["output_tokens"] / 1e6 * PRICE_OUT


class ZenChat:
    """OpenCode Zen 真实 LLM 适配器，支持 chat/completions 与 responses 两种协议。

    接口与 FakeLLM 一致（stream_chat 生成器 + calls/failed_calls/usage 属性），
    直接复用 run_scenario 的指标聚合。key 来源：OPENCODE_API_KEY / ZEN_API_KEY /
    OPENAI_API_KEY / HERO_API_KEY；base URL 可用 ZEN_BASE_URL 覆盖。
    Go 订阅端点（/zen/go/v1）模型走 Responses API（如 muse-spark-1.3-contributor）。
    """

    def __init__(self, model: str, api_key: str, base_url: str,
                 temperature: float = 0.2, api_style: str = "auto", session_id: str | None = None):
        from openai import OpenAI

        self.model = model
        self.calls = 0
        self.failed_calls = 0
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        headers = {"User-Agent": "hero-quant-eval/0.3.0"}  # Go 计划要求专属 UA
        if session_id:  # Go 计划要求稳定会话头（路由优化 + prompt 缓存）
            headers["x-opencode-session"] = session_id
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=90.0,
                              default_headers=headers)
        self._temperature = temperature
        if api_style == "auto":
            api_style = "responses" if ("/go/" in base_url or "muse-spark" in model) else "chat"
        self._api_style = api_style

    def _stream_chat_completions(self, goal: str):
        stream = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": goal}],
            stream=True,
            stream_options={"include_usage": True},
            temperature=self._temperature,
        )
        for event in stream:
            if getattr(event, "usage", None):
                self.usage["input_tokens"] += int(event.usage.prompt_tokens or 0)
                self.usage["output_tokens"] += int(event.usage.completion_tokens or 0)
            for choice in (event.choices or []):
                delta = getattr(choice, "delta", None)
                text = getattr(delta, "content", None) if delta else None
                if text:
                    yield {"type": "text", "text": text}

    def _stream_responses(self, goal: str):
        stream = self._client.responses.create(
            model=self.model,
            input=[{"role": "user", "content": goal}],
            stream=True,
        )
        for event in stream:
            etype = getattr(event, "type", "")
            if etype == "response.output_text.delta":
                text = getattr(event, "delta", "")
                if text:
                    yield {"type": "text", "text": text}
            elif etype == "response.completed":
                u = getattr(getattr(event, "response", None), "usage", None)
                if u:
                    self.usage["input_tokens"] += int(getattr(u, "input_tokens", 0) or 0)
                    self.usage["output_tokens"] += int(getattr(u, "output_tokens", 0) or 0)

    def stream_chat(self, goal: str):
        self.calls += 1
        try:
            if self._api_style == "responses":
                yield from self._stream_responses(goal)
            else:
                yield from self._stream_chat_completions(goal)
        except Exception:
            self.failed_calls += 1
            raise


REAL_QUESTIONS = [
    "分析贵州茅台（600519）近一年股价走势的核心驱动因素",
    "比较沪深300与中证500的成分股风格差异",
    "什么是夏普比率？如何用它评估策略质量？",
    "解释均值回归策略的原理与适用市场环境",
    "简述最大回撤的计算方法及其风险含义",
    "市盈率估值法在周期股上为什么会失效？",
    "说明动量因子在A股市场的历史表现特征",
    "如何判断一个量化回测结果是否过拟合？",
    "分析美联储加息周期对新兴市场股市的传导机制",
    "解释PIT（point-in-time）数据在回测中的重要性",
    "什么是滑点与冲击成本？如何估算？",
    "简述多因子模型的构建流程",
    "RSI超买超卖信号在震荡市和趋势市的有效性差异",
    "分析换手率因子与收益率的经验关系",
    "解释风险平价组合的基本思想",
    "什么是幸存者偏差？如何避免？",
    "简述Black-Scholes期权定价模型的核心假设",
    "分析财报发布窗口的日历效应",
    "解释协整关系在配对交易中的应用",
    "如何评估一个交易信号的记忆与衰减速度？",
]


def run_real(model: str, runs: int, api_key: str, base_url: str, api_style: str = "auto") -> dict:
    """真实档：同一套聚合逻辑，接真实 LLM 跑 e2e 任务。"""
    import uuid

    session_id = str(uuid.uuid4())  # 一次评测 = 一个稳定会话（Go 端点要求）
    prompts = (REAL_QUESTIONS * (runs // len(REAL_QUESTIONS) + 1))[:runs]
    results, usage_totals = [], {"input_tokens": 0, "output_tokens": 0}
    retries = 0
    for i, goal in enumerate(prompts):
        llm = ZenChat(model=model, api_key=api_key, base_url=base_url,
                      api_style=api_style, session_id=session_id)
        loop = AgentLoop(llm=llm, max_iterations=5)
        t0 = time.perf_counter()
        try:
            r: LoopResult = loop.run(goal)
            reason, iters = r.reason, r.iterations
            wall = time.perf_counter() - t0
        except Exception as exc:  # 连真实 SDK 异常也计入指标，不让单次失败中断评测
            reason, iters = f"exception:{type(exc).__name__}", 0
            wall = time.perf_counter() - t0
        results.append({"reason": reason, "iterations": iters, "wall_s": round(wall, 3),
                        "goal": goal[:40], "answer": getattr(r, "text", "")[:4000]})
        retries += llm.failed_calls
        for k in usage_totals:
            usage_totals[k] += llm.usage[k]
    reasons: dict[str, int] = {}
    for r in results:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    walls = sorted(r["wall_s"] for r in results)
    p = lambda q: walls[min(len(walls) - 1, int(q * len(walls)))]
    return {
        "scenario": "S0_real_e2e",
        "model": model,
        "base_url": base_url,
        "runs": runs,
        "reason_distribution": reasons,
        "success_rate": round(reasons.get("completed", 0) / runs, 4),
        "wall_s": {"p50": round(p(0.5), 3), "p95": round(p(0.95), 3), "mean": round(statistics.fmean(walls), 3)},
        "llm_retry_failures": retries,
        "usage_total": usage_totals,
        "cost_usd_total": round(cost_usd(usage_totals), 6),
        "cost_usd_per_run": round(cost_usd(usage_totals) / runs, 6),
    }


def run_scenario(name, runs, make_loop, goal="eval task"):
    results, retries, usage_totals = [], 0, {"input_tokens": 0, "output_tokens": 0}
    for _ in range(runs):
        loop, llm = make_loop()
        t0 = time.perf_counter()
        r: LoopResult = loop.run(goal)
        wall = time.perf_counter() - t0
        results.append({
            "reason": r.reason, "terminated": bool(r.terminated), "iterations": r.iterations,
            "wall_s": round(wall, 4), "token_count": r.token_count,
            "grounding_verified": bool(r.grounding_verified),
        })
        retries += llm.failed_calls  # 实际抛错并触发重试策略的调用次数
        for k in usage_totals:
            usage_totals[k] += llm.usage[k]
    reasons = {}
    for r in results:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    walls = sorted(r["wall_s"] for r in results)
    iters = [r["iterations"] for r in results]
    p = lambda q: walls[min(len(walls) - 1, int(q * len(walls)))]
    return {
        "scenario": name, "runs": runs,
        "reason_distribution": reasons,
        "success_rate": round(reasons.get("completed", 0) / runs, 4),
        "wall_s": {"mean": round(statistics.fmean(walls), 4), "p50": round(p(0.5), 4), "p95": round(p(0.95), 4)},
        "iterations": {"mean": round(statistics.fmean(iters), 2), "max": max(iters)},
        "retries_total": retries,
        "usage_total": usage_totals,
        "cost_usd_total": round(cost_usd(usage_totals), 6),
        "cost_usd_per_run": round(cost_usd(usage_totals) / runs, 6),
    }


def grounding_metrics(n=200, seed=42):
    """幻觉拦截率：编造价格应 100% 拦截，真实收盘价应 ~100% 通过。"""
    rng = random.Random(seed)
    ledger = GroundingLedger()
    closes = [10.0, 10.1, 10.2, 9.95, 10.05]
    ledger.ingest("600519", [{"close": c} for c in closes])
    blocked = passed = 0
    for _ in range(n):
        fake = round(rng.uniform(1, 100), 2)
        if any(abs(fake - c) < 0.5 for c in closes):
            continue
        try:
            ledger.assert_price("600519", fake)
            passed += 1  # 编造价未被拦截（漏网）
        except Exception:
            blocked += 1
    legit_ok = sum(1 for c in closes * (n // len(closes)) if _try(ledger, "600519", c))
    legit_n = len(closes) * (n // len(closes))
    return {
        "fabricated_samples": blocked + passed,
        "fabricated_blocked": blocked,
        "hallucination_block_rate": round(blocked / max(1, blocked + passed), 4),
        "legit_samples": legit_n,
        "legit_pass_rate": round(legit_ok / max(1, legit_n), 4),
    }


def _try(ledger, sym, price):
    try:
        ledger.assert_price(sym, price)
        return True
    except Exception:
        return False


def _resolve_key() -> str | None:
    import os

    for name in ("OPENCODE_API_KEY", "ZEN_API_KEY", "OPENAI_API_KEY", "HERO_API_KEY"):
        v = os.environ.get(name, "").strip()
        if v:
            return v
    return None


def main():
    import os

    global PRICE_IN, PRICE_OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--out", default="eval_report.json")
    ap.add_argument("--real", action="store_true", help="接真实 LLM（OpenCode Zen OpenAI 兼容端点）")
    ap.add_argument("--model", default="big-pickle", help="Zen 模型 id，默认免费模型 big-pickle")
    ap.add_argument("--list-models", action="store_true", help="列出 Zen 可用模型后退出")
    ap.add_argument("--base-url", default=os.environ.get("ZEN_BASE_URL", "https://opencode.ai/zen/v1"))
    ap.add_argument("--api", default="auto", choices=["auto", "chat", "responses"],
                    help="API 协议：Go 订阅端点模型用 responses，标准端点用 chat")
    args = ap.parse_args()

    # 定价与环境变量对齐 BudgetBreaker 口径（HERO_LLM_PRICE_IN/OUT 覆盖默认值）
    try:
        PRICE_IN = float(os.environ.get("HERO_LLM_PRICE_IN", "") or PRICE_IN)
        PRICE_OUT = float(os.environ.get("HERO_LLM_PRICE_OUT", "") or PRICE_OUT)
    except ValueError:
        pass

    if args.list_models or args.real:
        key = _resolve_key()
        if not key:
            print("缺少 API key：请先设置环境变量，例如 $env:OPENCODE_API_KEY=\"<你的 Zen key>\"", file=sys.stderr)
            sys.exit(2)
        if args.list_models:
            from openai import OpenAI

            for m in OpenAI(api_key=key, base_url=args.base_url).models.list().data:
                print(m.id)
            return

    if args.real:
        res = run_real(args.model, args.runs, api_key=key, base_url=args.base_url, api_style=args.api)  # type: ignore[name-defined]
        report = {
            "meta": {
                "mode": f"real（真实 LLM e2e，model={args.model}）",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "pricing_per_1m": {"input": PRICE_IN, "output": PRICE_OUT},
                "runs_per_scenario": args.runs,
            },
            "scenarios": [res],
        }
        out = ROOT / args.out
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        s = res
        print(f"# eval report -> {out}")
        print(f"\n[{s['scenario']}] model={s['model']} success={s['success_rate']:.0%} "
              f"wall p50/p95={s['wall_s']['p50']}s/{s['wall_s']['p95']}s "
              f"tokens(in/out)={s['usage_total']['input_tokens']}/{s['usage_total']['output_tokens']} "
              f"cost/run=${s['cost_usd_per_run']}")
        print(f"  reasons: {s['reason_distribution']}")
        return

    n = args.runs

    normal_chunks = [{"type": "text", "text": "分析完成：600519 收盘 10.0 元，趋势稳定。",
                      "usage_metadata": {"input_tokens": 120, "output_tokens": 80}}]
    huge_chunks = [{"type": "text", "text": "x" * 200_000,
                    "usage_metadata": {"input_tokens": 50_000, "output_tokens": 50_000}}]
    heavy_chunks = [{"type": "text", "text": "ok",
                     "usage_metadata": {"input_tokens": 20_000, "output_tokens": 20_000}}]
    empty_chunks = [{"type": "text", "text": ""}]
    bad_tool_chunks = [{"type": "tool_call", "name": "__no_such_tool__", "arguments": {}}]

    def mk_normal():
        llm = FakeLLM(normal_chunks)
        return AgentLoop(llm=llm, max_iterations=5), llm

    def mk_retry():
        llm = FakeLLM(normal_chunks, fail_times=1)
        return AgentLoop(llm=llm, max_iterations=5,
                         retry_policy=RetryPolicy(max_attempts=3, backoff_base=0.01, jitter=0.0)), llm

    def mk_fail():
        llm = FakeLLM(normal_chunks, fail_forever=True)
        return AgentLoop(llm=llm, max_iterations=5,
                         retry_policy=RetryPolicy(max_attempts=3, backoff_base=0.01, jitter=0.0)), llm

    def mk_empty():
        llm = FakeLLM(empty_chunks)
        return AgentLoop(llm=llm, max_iterations=5), llm

    def mk_bad_tool():
        llm = FakeLLM(bad_tool_chunks)
        return AgentLoop(llm=llm, max_iterations=5), llm

    def mk_token_limit():
        llm = FakeLLM(huge_chunks)
        return AgentLoop(llm=llm, max_iterations=5, token_limit=2000), llm

    def mk_budget():
        llm = FakeLLM(heavy_chunks)
        return AgentLoop(llm=llm, max_iterations=5,
                         budget_breaker=BudgetBreaker(daily_limit=0.001)), llm

    scenarios = [
        run_scenario("S1_normal_completion", n, mk_normal),
        run_scenario("S2_transient_retry", n, mk_retry),
        run_scenario("S3_llm_persistent_failure", max(5, n // 2), mk_fail),
        run_scenario("S4a_empty_loop_max_iterations", max(5, n // 2), mk_empty),
        run_scenario("S4b_unknown_tool", max(5, n // 2), mk_bad_tool),
        run_scenario("S5_token_limit", max(5, n // 2), mk_token_limit),
        run_scenario("S6_budget_circuit_break", max(5, n // 2), mk_budget),
    ]

    report = {
        "meta": {
            "mode": "mock（脚本化 LLM + 故障注入，确定性可复现，未调用真实 LLM）",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "python": platform.python_version(),
            "pricing_per_1m": {"input": PRICE_IN, "output": PRICE_OUT},
            "runs_per_scenario": n,
        },
        "scenarios": scenarios,
        "grounding": grounding_metrics(),
    }
    out = ROOT / args.out
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"# eval report -> {out}")
    for s in scenarios:
        print(f"\n[{s['scenario']}] success={s['success_rate']:.0%} "
              f"wall p50/p95={s['wall_s']['p50']}s/{s['wall_s']['p95']}s "
              f"iters={s['iterations']['mean']} retries={s['retries_total']} "
              f"cost/run=${s['cost_usd_per_run']}")
        print(f"  reasons: {s['reason_distribution']}")
    g = report["grounding"]
    print(f"\n[grounding] 幻觉拦截率={g['hallucination_block_rate']:.0%} "
          f"({g['fabricated_blocked']}/{g['fabricated_samples']}) "
          f"真实价通过率={g['legit_pass_rate']:.0%}")


if __name__ == "__main__":
    main()
