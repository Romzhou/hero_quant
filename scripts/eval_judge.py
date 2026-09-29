"""Agent 判官评测（LLM-as-judge）— 准确性/相关性/完整性 + 程序化合规检查。

流程：真实 LLM 生成 10 题回答（经 AgentLoop，与 e2e 口径一致）
     → 判官模型按 rubric 结构化打分（1-5 分 × 3 维度 + 幻觉风险标志）
     → 程序化合规检查（涉及投资建议是否带风险提示）
     → 聚合均分/通过率/分布，落盘 eval_judge_report.json（含逐题明细，可复现可复审）。

用法（项目根目录）:
  $env:OPENCODE_API_KEY="<key>"
  python scripts/eval_judge.py --model muse-spark-1.3-contributor --base-url https://opencode.ai/zen/go/v1 --api responses --runs 10
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

from eval_agent import REAL_QUESTIONS, ZenChat, cost_usd  # noqa: E402
from hero_quant.agent.loop import AgentLoop  # noqa: E402

JUDGE_SYSTEM = """你是严格的投资研究问答评测判官。对给定「问题」和「回答」按三个维度打分（1-5 整数）：
- accuracy 事实准确性：5=事实全部正确且无编造；4=基本正确有微小瑕疵；3=部分正确有实质错误；2=多处错误；1=核心事实错误或严重编造
- relevance 相关性：5=完全切题直击要点；4=切题略有偏移；3=部分偏题；2=大多偏题；1=答非所问
- completeness 完整性：5=覆盖问题全部关键要点且有深度；4=覆盖多数要点；3=覆盖一半；2=只覆盖少数；1=严重缺失
同时给出 hallucination_risk（true=回答包含无法从公认知识验证的具体数字/事件，false=否则）和 comment（一句话中文理由）。
只输出 JSON，不要输出任何其他文字：{"accuracy": int, "relevance": int, "completeness": int, "hallucination_risk": bool, "comment": str}"""

# 程序化合规检查：出现明确投资动作建议时，应包含风险提示
_ADVICE_PAT = re.compile(r"(建议(买入|卖出|加仓|减仓|清仓)|应当(买入|卖出)|值得买入|立即买入|全仓|梭哈)")
_RISK_PAT = re.compile(r"(风险|不构成投资建议|仅供参考|谨慎|自行判断)")


def _extract_json(text: str) -> dict:
    """从判官输出提取 JSON（容忍 ```json 围栏与前后杂文本）。"""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON in judge output: {text[:200]!r}")
    return json.loads(m.group(0))


def generate_answers(model, api_key, base_url, api_style, runs):
    """经 AgentLoop 生成回答（与 e2e 评测同口径），落盘文本与指标。"""
    session_id = str(uuid.uuid4())
    prompts = (REAL_QUESTIONS * (runs // len(REAL_QUESTIONS) + 1))[:runs]
    items, usage = [], {"input_tokens": 0, "output_tokens": 0}
    for i, goal in enumerate(prompts):
        llm = ZenChat(model=model, api_key=api_key, base_url=base_url,
                      api_style=api_style, session_id=session_id)
        loop = AgentLoop(llm=llm, max_iterations=5)
        t0 = time.perf_counter()
        try:
            r = loop.run(goal)
            answer, reason, wall = r.text, r.reason, time.perf_counter() - t0
        except Exception as exc:
            answer, reason, wall = "", f"exception:{type(exc).__name__}", time.perf_counter() - t0
        for k in usage:
            usage[k] += llm.usage[k]
        items.append({"idx": i, "question": goal, "answer": answer,
                      "reason": reason, "wall_s": round(wall, 3)})
        print(f"  [{i+1}/{runs}] {reason} {wall:.1f}s {len(answer)} chars")
    return items, usage


def judge_answers(model, api_key, base_url, api_style, items):
    """逐题判分。判官与被测同一模型——报告须注明此局限（自评偏差）。"""
    session_id = str(uuid.uuid4())
    for it in items:
        if not it["answer"]:
            it["judge"] = None
            continue
        judge = ZenChat(model=model, api_key=api_key, base_url=base_url,
                        api_style=api_style, session_id=session_id)
        user_msg = f"问题：{it['question']}\n\n回答：{it['answer'][:6000]}"
        try:
            stream = judge._client.responses.create(
                model=model, stream=True,
                input=[{"role": "system", "content": JUDGE_SYSTEM},
                       {"role": "user", "content": user_msg}],
            )
            buf = []
            for ev in stream:
                if getattr(ev, "type", "") == "response.output_text.delta":
                    buf.append(getattr(ev, "delta", ""))
                elif getattr(ev, "type", "") == "response.completed":
                    u = getattr(getattr(ev, "response", None), "usage", None)
                    if u:
                        judge.usage["input_tokens"] += int(getattr(u, "input_tokens", 0) or 0)
                        judge.usage["output_tokens"] += int(getattr(u, "output_tokens", 0) or 0)
            it["judge"] = _extract_json("".join(buf))
        except Exception as exc:
            it["judge"] = {"error": f"{type(exc).__name__}: {exc}"}
        j = it["judge"] or {}
        print(f"  [judge {it['idx']+1}] acc={j.get('accuracy')} rel={j.get('relevance')} "
              f"comp={j.get('completeness')} halluc={j.get('hallucination_risk')}")


def compliance_check(items):
    """程序化合规：明确投资动作建议需伴随风险提示。"""
    for it in items:
        a = it["answer"] or ""
        has_advice = bool(_ADVICE_PAT.search(a))
        has_risk = bool(_RISK_PAT.search(a))
        it["compliance"] = "pass" if (not has_advice or has_risk) else "violation"


def aggregate(items, usage, gen_seconds, model):
    scored = [it for it in items if isinstance(it.get("judge"), dict) and "accuracy" in it["judge"]]
    errors = [it for it in items if isinstance(it.get("judge"), dict) and "error" in it["judge"]]

    def dim(name):
        vals = [it["judge"][name] for it in scored]
        return {"mean": round(statistics.fmean(vals), 2) if vals else None,
                "dist": {str(v): vals.count(v) for v in sorted(set(vals))}}

    acc_vals = [it["judge"]["accuracy"] for it in scored]
    rel_vals = [it["judge"]["relevance"] for it in scored]
    comp_vals = [it["judge"]["completeness"] for it in scored]
    halluc = sum(1 for it in scored if it["judge"].get("hallucination_risk"))
    violations = sum(1 for it in items if it.get("compliance") == "violation")
    return {
        "model": model,
        "questions": len(items),
        "scored": len(scored),
        "judge_errors": len(errors),
        "accuracy": {**dim("accuracy"),
                     "pass_rate_4plus": round(sum(1 for v in acc_vals if v >= 4) / max(1, len(acc_vals)), 3)},
        "relevance": {**dim("relevance"),
                      "pass_rate_4plus": round(sum(1 for v in rel_vals if v >= 4) / max(1, len(rel_vals)), 3)},
        "completeness": {**dim("completeness"),
                         "pass_rate_4plus": round(sum(1 for v in comp_vals if v >= 4) / max(1, len(comp_vals)), 3)},
        "hallucination_flagged": halluc,
        "compliance_violations": violations,
        "gen_wall_s_total": round(gen_seconds, 1),
        "usage_total": usage,
        "cost_usd_total": round(cost_usd(usage), 6),
        "caveat": "判官与被测为同一模型（自评偏差），通过率仅供相对比较；逐题明细已落盘供人工复核。",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--model", default="muse-spark-1.3-contributor")
    ap.add_argument("--base-url", default="https://opencode.ai/zen/go/v1")
    ap.add_argument("--api", default="responses", choices=["chat", "responses"])
    ap.add_argument("--key-env", default="OPENCODE_API_KEY")
    ap.add_argument("--out", default="eval_judge_report.json")
    ap.add_argument("--answers-in", default=None, help="已有回答 JSON 则跳过生成直接判分")
    args = ap.parse_args()

    import os

    key = os.environ.get(args.key_env, "").strip()
    if not key:
        print(f"缺少 API key：请设置 $env:{args.key_env}", file=sys.stderr)
        sys.exit(2)

    if args.answers_in and Path(args.answers_in).exists():
        items = json.loads(Path(args.answers_in).read_text(encoding="utf-8"))["items"]
        usage, gen_seconds = {"input_tokens": 0, "output_tokens": 0}, 0.0
        print(f"# 载入已有回答 {len(items)} 题，跳过生成")
    else:
        print(f"# 生成 {args.runs} 题回答（{args.model}）")
        t0 = time.perf_counter()
        items, usage = generate_answers(args.model, key, args.base_url, args.api, args.runs)
        gen_seconds = time.perf_counter() - t0

    print("# 判官评分中")
    judge_answers(args.model, key, args.base_url, args.api, items)

    print("# 程序化合规检查")
    compliance_check(items)

    report = {
        "meta": {
            "mode": "real（真实 LLM 生成 + 同模型判官 rubric 打分 + 程序化合规检查）",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "judge_model": args.model,
        },
        "summary": aggregate(items, usage, gen_seconds, args.model),
        "items": items,
    }
    out = ROOT / args.out
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    s = report["summary"]
    print(f"\n# eval judge report -> {out}")
    print(f"accuracy    mean={s['accuracy']['mean']} pass≥4={s['accuracy']['pass_rate_4plus']:.0%} dist={s['accuracy']['dist']}")
    print(f"relevance   mean={s['relevance']['mean']} pass≥4={s['relevance']['pass_rate_4plus']:.0%} dist={s['relevance']['dist']}")
    print(f"completeness mean={s['completeness']['mean']} pass≥4={s['completeness']['pass_rate_4plus']:.0%} dist={s['completeness']['dist']}")
    print(f"hallucination_flagged={s['hallucination_flagged']} compliance_violations={s['compliance_violations']}")


if __name__ == "__main__":
    main()
