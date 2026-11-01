"""python -m hero_quant.runs —— 队列 worker 进程入口。

用包级 __main__ 而不是 worker.py 的 __main__ 守卫：runs/__init__ 会 import worker，
用 -m hero_quant.runs.worker 时 runpy 会报 "found in sys.modules ... unpredictable behaviour"。
"""

from __future__ import annotations

from hero_quant.runs.cli import main

if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
