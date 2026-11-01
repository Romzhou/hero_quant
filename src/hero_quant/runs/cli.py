"""runs CLI — 队列 worker 进程入口（批次14 接线）。

用法：
    python -m hero_quant.runs.worker --handler my_pkg.jobs:handle_backtest
    python -m hero_quant.runs.worker --handler ... --once      # 只跑一轮，便于 cron / 冒烟
    python -m hero_quant.runs.worker --handler ... --poll 2 --lease 300 --heartbeat 30

设计（fail-closed）：
- **必须显式给 handler**：没有 handler 就直接 exit 2，绝不用「什么也不做」的默认处理器去
  空转领任务——那会把 queued 任务领走再失败，烧掉 max_tries。
- DSN 走 get_pool 的既有解析（settings 网关）；连不上就抛，不静默降级成内存队列。
- SIGTERM/SIGINT 走协作式停止：设置 stop 标志，当前任务做完再退出（不打断进行中的 LLM 调用）。
"""

from __future__ import annotations

import argparse
import importlib
import logging
import signal
import sys
from collections.abc import Callable, Sequence
from typing import Any

from hero_quant.runs.store import RunQueue
from hero_quant.runs.worker import RunWorker

logger = logging.getLogger("hero_quant.runs")


def load_handler(spec: str) -> Callable[[dict, Any], Any]:
    """把 "pkg.module:func" 解析成可调用对象；格式非法或属性缺失一律抛错（不吞）。"""
    if ":" not in spec:
        raise ValueError(f"handler must be 'module:function', got {spec!r}")
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise ValueError(f"handler must be 'module:function', got {spec!r}")
    module = importlib.import_module(module_name)
    handler = getattr(module, attr, None)
    if handler is None or not callable(handler):
        raise ValueError(f"handler {spec!r} is not a callable attribute")
    return handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hero_quant.runs",
        description="长任务队列 worker（PR-201~203）。必须显式指定 handler；不指定直接退出。",
    )
    parser.add_argument("--handler", help="pkg.module:function —— 任务处理器（除 --reap 外必填）")
    parser.add_argument("--reap", action="store_true", help="只跑一次过期租约回收（看门狗），不起 worker")
    parser.add_argument("--once", action="store_true", help="只跑一轮（有活返回 0，没活也返回 0）")
    parser.add_argument("--poll", type=float, default=1.0, help="空闲轮询间隔秒（默认 1.0）")
    parser.add_argument("--lease", type=int, default=300, help="租约秒（默认 300）")
    parser.add_argument("--heartbeat", type=float, default=30.0, help="心跳间隔秒（默认 30）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.reap:
        queue = RunQueue.from_worker_dsn()
        result = queue.reap_expired()
        print(f"reap: {result}")
        return 0
    if not args.handler:
        print(
            "missing --handler: refusing to start a worker without a job handler "
            "(an empty handler would claim runs and burn their retries)",
            file=sys.stderr,
        )
        return 2
    try:
        handler = load_handler(args.handler)
    except (ImportError, ValueError) as exc:
        print(f"cannot load handler: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    queue = RunQueue.from_worker_dsn()
    worker = RunWorker(
        queue,
        handler,
        lease_seconds=args.lease,
        heartbeat_interval=args.heartbeat,
        poll_interval=args.poll,
    )

    def _stop(signum: int, _frame: Any) -> None:
        logger.warning("runs.worker_stop_requested signal=%s", signum)
        worker.request_stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if args.once:
        worker.run_once()
        return 0
    worker.run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
