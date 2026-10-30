#!/usr/bin/env python3
"""采集一份小样本日线快照并落 data_snapshots + pit_bars（W3 PR-121/122）。

用法：
    python scripts/snapshot_sample.py --tenant acme --symbols 000001.SZ,600519.SH \
        --start 2024-01-01 --end 2024-12-31 [--adjust qfq] [--json]

行为：
- 经 MarketDataRegistry（tencent 主源，yahoo 可选备源）取数；
- `capture_snapshot` 规范化 bars、算内容 sha256、写 PG 快照；
- 只打印 snapshot_id / row_count / storage_uri 等非敏感信息；
- 无 PG 或无数据源时 fail-closed（退出码 2），不写半份快照。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from hero_quant.data.registry import MarketDataRegistry
from hero_quant.data.snapshot import SnapshotError, SnapshotStore, capture_snapshot

logger = logging.getLogger(__name__)


def _build_registry() -> MarketDataRegistry:
    registry = MarketDataRegistry()
    from hero_quant.data.loaders.tencent import TencentLoader

    registry.register(TencentLoader())
    try:
        from hero_quant.data.loaders.yahoo import YahooLoader

        registry.register(YahooLoader())
    except ImportError as exc:
        logger.info("yahoo loader unavailable, tencent only: %s", exc)
    return registry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="采集小样本日线快照")
    parser.add_argument("--tenant", required=True, help="租户 ID（快照归属）")
    parser.add_argument("--symbols", required=True, help="逗号分隔标的，如 000001.SZ,600519.SH")
    parser.add_argument("--start", required=True, help="起始日 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="结束日 YYYY-MM-DD")
    parser.add_argument("--adjust", default="qfq", help="复权口径（默认 qfq）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
    if not symbols:
        print("no symbols", file=sys.stderr)
        return 2

    try:
        store = SnapshotStore.from_dsn()
    except SnapshotError as exc:
        print(f"snapshot store unavailable: {exc.reason}", file=sys.stderr)
        return 2

    try:
        snapshot = capture_snapshot(
            store,
            args.tenant,
            _build_registry(),
            symbols,
            args.start,
            args.end,
            adjust=args.adjust,
        )
    except SnapshotError as exc:
        print(f"snapshot failed: {exc.reason}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 数据源异常统一转退出码，不写半份快照
        print(f"data fetch failed: {type(exc).__name__}", file=sys.stderr)
        return 2

    summary = {
        "snapshot_id": snapshot["snapshot_id"],
        "sha256": snapshot["sha256"],
        "row_count": snapshot["row_count"],
        "symbols": snapshot["symbols"],
        "start_date": snapshot["start_date"],
        "end_date": snapshot["end_date"],
        "adjust": snapshot["adjust"],
        "storage_uri": snapshot["storage_uri"],
        "created": snapshot.get("created"),
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(
            f"snapshot={summary['snapshot_id']} rows={summary['row_count']} "
            f"symbols={','.join(summary['symbols'])} created={summary['created']} "
            f"uri={summary['storage_uri']}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
