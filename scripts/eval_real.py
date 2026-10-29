#!/usr/bin/env python
"""真模型 E2E 入口（W2 · PR-112）。

读 `evals/golden/golden_v1.jsonl`，通过 provider 网关逐条跑 AgentLoop，
记录 provider / tokens / 成本 / 耗时，写 `evals/results_golden_real.json`。

无可用 key 或后端不可达时**如实 skip**（写 `status=skipped, reason=no_llm_backend`），
不产出任何通过率数字——这是 AGENTS §6「降级必须显式」的硬约束。

用法：
    python scripts/eval_real.py
    python scripts/eval_real.py --limit 5 --out /tmp/eval.json
    python scripts/eval_real.py --min-pass-rate 0.8     # CI 回归门（可选）
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from evals.run_golden import main  # noqa: E402  —— sys.path 引导后导入

if __name__ == "__main__":
    raise SystemExit(main(["--mode", "real", *sys.argv[1:]]))
