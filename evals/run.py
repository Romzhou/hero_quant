"""P线 Agent 评测集一键重跑入口。

fake 模式（默认，无需任何 key）：
    python evals/run.py --mode fake

real 模式（需要真实 LLM key，否则自动跳过）：
    python evals/run.py --mode real

每个用例做三层断言（对应 dataset.jsonl 字段）：
1. expected_tools      — 是否存在于 tools/registry TOOL_REGISTRY（防幻觉工具名）；
2. expected_structure  — fake 可验证的 markers 是否在 loop 输出中出现；
                        spec-only markers（risk_warning / refusal_or_clarify /
                        tool_trace）在 fake 模式下只记录不判失败；
3. must_contain        — 关键数字/标注是否出现在 loop 输出中。

fake_expect=known_gap 的用例预期在 fake 模式下部分失败（记录降级缺口，
不计入 headline 通过率，但计入失败分类）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATASET = HERE / "dataset.jsonl"

# fake 模式下可验证的结构 markers -> 输出中应出现的子串
FAKE_CHECKABLE = {
    "symbol_header": "600519.SH",
    "price_number": "1680.2",
    "metrics_table": "sharpe",  # fake 模板固定携带 metrics/sharpe 行
    "grounding_flag": "grounding_verified",
    "degraded_banner": "演示模式",
    "backtest_numbers": "1.62",
    "risk_warning": "不构成投资建议",
}
# 仅 spec 层面的 markers：fake LLM 永远产不出，不判失败，只记录缺口
SPEC_ONLY = {"risk_warning", "refusal_or_clarify", "tool_trace"}

STRUCTURE_VOCAB = set(FAKE_CHECKABLE) | SPEC_ONLY


class FakeLLM:
    """与 src/hero_quant/api/server.py::_FakeLLM 同模板（链A降级口径一致）。"""

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


def load_dataset():
    cases = []
    with open(DATASET, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"[dataset] line {ln} JSON invalid: {e}", file=sys.stderr)
                sys.exit(2)
    return cases


def registry_names():
    import hero_quant.tools.backtest  # noqa: F401
    import hero_quant.tools.correlation  # noqa: F401
    import hero_quant.tools.market_data  # noqa: F401
    import hero_quant.tools.presentation  # noqa: F401
    import hero_quant.tools.quantlib_tool  # noqa: F401
    from hero_quant.tools.registry import TOOL_REGISTRY

    return set(TOOL_REGISTRY.keys())


def run_fake_loop(query: str) -> dict:
    from hero_quant.agent.loop import AgentLoop

    loop = AgentLoop(llm=FakeLLM(), max_iterations=2, trace=False)
    t0 = time.monotonic()
    try:
        res = loop.run(query)
        return {
            "ok": True,
            "text": res.text or "",
            "reason": res.reason or "",
            "terminated": bool(res.terminated),
            "elapsed_s": round(time.monotonic() - t0, 3),
        }
    except Exception as e:  # noqa: BLE001 — 评测需收敛所有异常为失败分类
        return {
            "ok": False,
            "text": "",
            "reason": f"exception:{type(e).__name__}:{e}",
            "terminated": False,
            "elapsed_s": round(time.monotonic() - t0, 3),
        }


def eval_case(case: dict, tool_names: set, mode: str) -> dict:
    cid = case.get("id", "?")
    failures: list[str] = []
    notes: list[str] = []

    # 1) 工具路由断言：期望工具必须真实注册
    for t in case.get("expected_tools", []):
        if t not in tool_names:
            failures.append(f"tool_not_registered:{t}")
    # 结构词汇表断言
    for s in case.get("expected_structure", []):
        if s not in STRUCTURE_VOCAB:
            failures.append(f"unknown_structure:{s}")

    text = ""
    if mode == "real":
        # 真 LLM 输出不可预知模板：只做连通性健康断言，数字/结构只记录不判失败
        out = run_fake_loop(case.get("query", ""))
        text = out["text"]
        if not out["ok"]:
            failures.append(f"loop_exception:{out['reason']}")
        if not out["terminated"]:
            failures.append("not_terminated")
        if not text.strip():
            failures.append("empty_text")
        notes.append(f"real_text_len:{len(text)}")
        for token in case.get("must_contain", []):
            notes.append(f"real_must_contain:{token}:{'hit' if token in text else 'miss'}")
    if mode == "fake":
        out = run_fake_loop(case.get("query", ""))
        text = out["text"]
        if not out["ok"]:
            failures.append(f"loop_exception:{out['reason']}")
        if not out["terminated"]:
            failures.append("not_terminated")
        if not out["reason"]:
            failures.append("empty_reason")
        # 2) 结构断言（仅 fake 可验证部分）
        for s in case.get("expected_structure", []):
            if s in SPEC_ONLY:
                marker = FAKE_CHECKABLE.get(s)
                present = bool(marker and marker in text) if marker else False
                notes.append(f"spec_only:{s}:{'present' if present else 'absent_in_fake'}")
            elif s in FAKE_CHECKABLE:
                if FAKE_CHECKABLE[s] not in text:
                    failures.append(f"structure_missing:{s}")
        # 3) 数字断言
        for token in case.get("must_contain", []):
            if token not in text:
                failures.append(f"number_missing:{token}")
    fake_expect = case.get("fake_expect", "pass")
    passed = not failures
    return {
        "id": cid,
        "category": case.get("category", "?"),
        "fake_expect": fake_expect,
        "passed": passed,
        "failures": failures,
        "notes": notes,
        "known_gap": fake_expect == "known_gap",
    }


def classify(failures: list[str]) -> str:
    if not failures:
        return "pass"
    joined = " ".join(failures)
    if "loop_exception" in joined or "not_terminated" in joined:
        return "loop_crash_or_hang"
    if "tool_not_registered" in joined or "unknown_structure" in joined:
        return "spec_invalid"
    if "structure_missing" in joined:
        return "structure_gap"
    if "number_missing" in joined:
        return "number_gap"
    return "other"


def main() -> int:
    ap = argparse.ArgumentParser(description="P线 Agent 评测集")
    ap.add_argument("--mode", choices=["fake", "real"], default="fake")
    ap.add_argument("--out", default=None, help="结果 JSON 输出路径（默认 evals/results_<mode>.json）")
    args = ap.parse_args()

    cases = load_dataset()
    print(f"[eval] cases={len(cases)} mode={args.mode}")
    tool_names = registry_names()
    print(f"[eval] registered_tools={len(tool_names)}")

    if args.mode == "real":
        from hero_quant.config.settings import Settings

        s = Settings()
        # 有 key 才跑真 LLM：此处复用 fake 评估管线但注入真 llm
        from hero_quant.agent.loop import AgentLoop
        from hero_quant.llm.factory import LLMFactory

        llm = LLMFactory(settings=s).create()

        def _is_offline_fallback(obj) -> bool:
            """factory 无 provider SDK 时内嵌 _FallbackChat（llm/factory.py:147），
            外层是 LLMClient 包装：递归内省，有一层 fallback 即判为离线。"""
            seen: set[int] = set()
            stack: list = [obj]
            while stack:
                o = stack.pop()
                if id(o) in seen:
                    continue
                seen.add(id(o))
                if type(o).__name__ in ("_FallbackChat", "FakeLLM", "_FakeLLM"):
                    return True
                try:
                    vals = vars(o).values()
                except TypeError:
                    continue
                stack.extend(v for v in vals if not isinstance(v, (str, int, float, bool, bytes, type(None))))
            return False

        # 无 provider SDK 时 factory 返回内嵌 _FallbackChat 的 LLMClient
        # （llm/factory.py:147）：这不是真 LLM，直接跳过而非硬跑，
        # 避免 38 个结构性误失败。
        if _is_offline_fallback(llm):
            print(f"[eval] SKIP real 模式：无可用 LLM 后端 (factory 返回 {type(llm).__name__}，缺 provider SDK/key)")
            print("[eval] real 结果：skipped（原因：no_llm_backend），fake 基线为准")
            out_path = Path(args.out) if args.out else HERE / "results_real.json"
            out_path.write_text(
                json.dumps(
                    {
                        "mode": "real",
                        "status": "skipped",
                        "reason": "no_llm_backend",
                        "detail": f"LLMFactory.create() returned {type(llm).__name__} wrapping "
                        "inner _FallbackChat (llm/factory.py:147); "
                        "no provider SDK installed, real-LLM assertions not meaningful",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"[eval] wrote {out_path}")
            return 0

        def run_real_loop(query: str) -> dict:
            loop = AgentLoop(llm=llm, max_iterations=2, trace=False)
            t0 = time.monotonic()
            try:
                res = loop.run(query)
                return {
                    "ok": True,
                    "text": res.text or "",
                    "reason": res.reason or "",
                    "terminated": bool(res.terminated),
                    "elapsed_s": round(time.monotonic() - t0, 3),
                }
            except Exception as e:  # noqa: BLE001
                return {
                    "ok": False,
                    "text": "",
                    "reason": f"exception:{type(e).__name__}:{e}",
                    "terminated": False,
                    "elapsed_s": round(time.monotonic() - t0, 3),
                }

        global _real_runner  # noqa: F841 — 保留扩展点
        _real_runner = run_real_loop
        # monkey-patch 本模块的 run_fake_loop 指向真 runner（保持断言逻辑一致）
        globals()["run_fake_loop"] = run_real_loop
        # real 模式只做连通性健康断言（eval_case mode="real"），真 LLM 应能补上 fake 的缺口
        results = [eval_case(c, tool_names, mode="real") for c in cases]
    else:
        results = [eval_case(c, tool_names, mode="fake") for c in cases]

    for r in results:
        r["failure_class"] = classify(r["failures"])

    headline = [r for r in results if not r["known_gap"]]
    gaps = [r for r in results if r["known_gap"]]
    hp = sum(1 for r in headline if r["passed"])
    gp = sum(1 for r in gaps if r["passed"])

    from collections import Counter

    classes = Counter(r["failure_class"] for r in results if not r["passed"])

    print(
        f"[eval] headline(headline, 不含known_gap): {hp}/{len(headline)} = {hp / len(headline) * 100:.1f}%"
        if headline
        else "[eval] no headline cases"
    )
    print(f"[eval] known_gap cases passed in fake: {gp}/{len(gaps)}")
    print(f"[eval] failure_classes: {dict(classes)}")
    for r in results:
        if not r["passed"]:
            print(f"  FAIL {r['id']} [{r['failure_class']}] " + "; ".join(r["failures"]))

    out_path = Path(args.out) if args.out else HERE / f"results_{args.mode}.json"
    payload = {
        "mode": args.mode,
        "total": len(results),
        "headline_total": len(headline),
        "headline_passed": hp,
        "headline_rate": round(hp / len(headline), 4) if headline else None,
        "known_gap_total": len(gaps),
        "known_gap_passed_in_fake": gp,
        "failure_classes": dict(classes),
        "results": results,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[eval] wrote {out_path}")
    return 0 if hp == len(headline) else 1


if __name__ == "__main__":
    raise SystemExit(main())
