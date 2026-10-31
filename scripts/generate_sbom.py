#!/usr/bin/env python3
"""生成 CycloneDX 1.5 JSON SBOM（纯标准库，无网络、无第三方工具）。

用法：
    python scripts/generate_sbom.py [--output sbom.json] [--timestamp 2026-01-01T00:00:00Z]

行为：
- 组件来源 = 当前解释器已安装的 Python 发行版（`importlib.metadata.distributions()`）；
  覆盖范围只到 Python 发行版，不含 OS 包 / npm / 容器基础镜像（见 `docs/SBOM.md`）。
- 只读发行版元数据里的 `Name` 与 `Version` 两个字段；不读 `.env`、配置、DSN、API key，
  输出中不含任何文件路径（`tests/test_generate_sbom.py` 有对应断言）。
- `serialNumber` 由「排序去重后的组件 + timestamp」派生 UUIDv5，同输入必得同字节输出；
  需要严格可复现时用 `--timestamp`，或用 `SOURCE_DATE_EPOCH`（秒）钉住时间。
- 退出码：0 成功；1 写文件失败（stderr 只给通用错误，不回显路径细节）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.metadata
import json
import os
import re
import sys
import uuid
from collections.abc import Iterable
from typing import Any

# CycloneDX 规范版本；升 1.6 前先确认下游消费方（见 docs/SBOM.md）
SPEC_VERSION = "1.5"
BOM_FORMAT = "CycloneDX"
# BOM 主体（metadata.component）。版本与 pyproject.toml 的 project.version 同步——
# 本脚本刻意不读文件/环境，所以不在这里做 pyproject 解析，改版本时改这一处 + pyproject。
COMPONENT_NAME = "hero-quant"
COMPONENT_VERSION = "0.3.0"
TOOL_NAME = "hero-quant-sbom"
TOOL_VERSION = "1.0.0"

# 唯一允许读取的环境变量：SOURCE_DATE_EPOCH（可复现构建的既有约定，秒级 epoch）
TIMESTAMP_ENV = "SOURCE_DATE_EPOCH"

# 固定命名空间：换掉它等于宣布「这是一份别的 SBOM」（serialNumber 会全变）
_SBOM_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://hero-quant.invalid/sbom")
# PEP 503 规范化：小写 + 连续 [-_.] 折叠成单个 '-'
_CANON_SEP = re.compile(r"[-_.]+")
_CANON_NAME = COMPONENT_NAME
_COMPONENT_PURL = f"pkg:pypi/{_CANON_NAME}@{COMPONENT_VERSION}"


def canonicalize_name(raw: str) -> str:
    """按 PEP 503 规范化发行版名（小写 + 折叠 `-_.`），与 PyPI purl 口径一致。"""
    return _CANON_SEP.sub("-", raw.strip()).lower()


def _iter_installed(distributions: Iterable[Any] | None = None) -> list[tuple[str, str]]:
    """枚举已安装发行版，返回按 (name, version) 排序去重的列表。

    跳过无名字/无版本/名字规范化后为空的发行版；BOM 主体（hero-quant 自身）由
    metadata.component 描述，这里排除，避免同一 bom-ref 出现两次。
    """
    dists = importlib.metadata.distributions() if distributions is None else distributions
    pairs: set[tuple[str, str]] = set()
    for dist in dists:
        meta = getattr(dist, "metadata", None)
        if meta is None:
            continue
        name = meta.get("Name")
        version = getattr(dist, "version", None)
        if not name or not version:
            continue
        pairs.add((canonicalize_name(str(name)), str(version).strip()))
    return sorted(pair for pair in pairs if pair[0] and pair[1] and pair[0] != _CANON_NAME)


def _component(name: str, version: str) -> dict[str, Any]:
    purl = f"pkg:pypi/{name}@{version}"
    return {"type": "library", "bom-ref": purl, "name": name, "version": version, "purl": purl}


def _normalize_timestamp(value: str) -> str:
    """把 ISO-8601 归一成 CycloneDX 口径的 UTC RFC3339（`...Z`，秒精度）。"""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve_timestamp(explicit: str | None = None, env: dict[str, str] | None = None) -> str:
    """确定 metadata.timestamp：显式参数 > SOURCE_DATE_EPOCH > 当前 UTC。"""
    if explicit:
        return _normalize_timestamp(explicit)
    environ = os.environ if env is None else env
    epoch = environ.get(TIMESTAMP_ENV, "").strip()
    if epoch:
        try:
            return _normalize_timestamp(dt.datetime.fromtimestamp(int(epoch), tz=dt.timezone.utc).isoformat())
        except (ValueError, OSError, OverflowError):
            # 脏 epoch 不阻塞出 SBOM：回落当前 UTC（可复现性由 CI 侧显式传参保证）
            pass
    return _normalize_timestamp(dt.datetime.now(tz=dt.timezone.utc).isoformat())


def build_sbom(*, timestamp: str, distributions: Iterable[Any] | None = None) -> dict[str, Any]:
    """组装 CycloneDX 1.5 BOM dict；纯函数（同输入同输出，不碰时钟/随机数）。"""
    components = [_component(name, version) for name, version in _iter_installed(distributions)]
    # serialNumber 只由组件与 timestamp 派生：不给随机 uuid4，保证两次生成可 diff
    seed = json.dumps({"timestamp": timestamp, "components": components}, sort_keys=True, separators=(",", ":"))
    return {
        "bomFormat": BOM_FORMAT,
        "specVersion": SPEC_VERSION,
        "serialNumber": "urn:uuid:" + str(uuid.uuid5(_SBOM_NAMESPACE, seed)),
        "version": 1,
        "metadata": {
            "timestamp": timestamp,
            "tools": {
                "components": [{"type": "application", "name": TOOL_NAME, "version": TOOL_VERSION}],
            },
            "component": {
                "type": "application",
                "bom-ref": _COMPONENT_PURL,
                "name": COMPONENT_NAME,
                "version": COMPONENT_VERSION,
                "purl": _COMPONENT_PURL,
            },
        },
        "components": components,
    }


def render_sbom(bom: dict[str, Any]) -> str:
    """序列化为稳定文本（键序 = 组装顺序，缩进 2，结尾换行）。"""
    return json.dumps(bom, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 CycloneDX 1.5 JSON SBOM（纯标准库，无网络）")
    parser.add_argument("--output", default="-", help="输出文件路径；'-'（默认）表示写 stdout")
    parser.add_argument(
        "--timestamp",
        default=None,
        help="ISO-8601 时间戳；默认取 SOURCE_DATE_EPOCH，未设则用当前 UTC",
    )
    args = parser.parse_args(argv)

    try:
        timestamp = _resolve_timestamp(args.timestamp)
    except ValueError:
        print("invalid --timestamp: expect ISO-8601 like 2026-01-01T00:00:00Z", file=sys.stderr)
        return 2

    payload = render_sbom(build_sbom(timestamp=timestamp))
    if args.output.strip() in ("", "-"):
        sys.stdout.write(payload)
        return 0

    try:
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
    except OSError as exc:
        # 不回显路径与堆栈（可能含 workspace/host 信息），只给通用原因
        print(f"failed to write SBOM: {exc.strerror or type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
