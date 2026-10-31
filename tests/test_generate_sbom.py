"""`scripts/generate_sbom.py` 的契约与确定性测试（PR-153 SBOM 部分）。

要点：
- 一律用 monkeypatch 替换 `importlib.metadata.distributions`，用例不依赖本机装了哪些包；
- 时间戳固定，故 serialNumber / 字节输出必须可复现；
- 断言输出不含密钥、DSN、文件路径等敏感形态（env 里塞真值也不许漏）。
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import pathlib
import sys
import uuid

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "generate_sbom.py"
FIXED_TS = "2026-01-01T00:00:00Z"

# 敏感形态黑名单：任何一项出现在 SBOM 里都视为泄漏
SENSITIVE_NEEDLES = ("ak-", "sk-", "postgresql://", "postgres://", "/site-packages/", "/root/", "HERO_")


def _load_script():
    spec = importlib.util.spec_from_file_location("hero_quant_generate_sbom", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sbom = _load_script()


class _StubDist:
    """最小发行版桩：只需 `.metadata.get("Name")` 与 `.version` 两个读口。"""

    def __init__(self, name: str, version: str | None, **extra: str) -> None:
        self.metadata = {"Name": name, **extra}
        self.version = version


@pytest.fixture
def stub_dists(monkeypatch):
    """装一批发行版桩，返回安装函数便于用例内二次覆盖。"""

    def _install(pairs: list[tuple[str, str]]) -> None:
        dists = [_StubDist(name, version) for name, version in pairs]
        monkeypatch.setattr(importlib.metadata, "distributions", lambda: iter(dists))

    return _install


def test_output_is_byte_identical_for_same_input(stub_dists):
    stub_dists([("zeta-lib", "1.0"), ("Alpha.Lib", "2.0")])
    first = sbom.render_sbom(sbom.build_sbom(timestamp=FIXED_TS))
    second = sbom.render_sbom(sbom.build_sbom(timestamp=FIXED_TS))
    assert first.encode("utf-8") == second.encode("utf-8")


def test_required_cyclonedx_fields(stub_dists):
    stub_dists([("alpha-lib", "1.0")])
    bom = sbom.build_sbom(timestamp=FIXED_TS)

    assert bom["bomFormat"] == "CycloneDX"
    assert bom["specVersion"] == "1.5"
    assert bom["version"] == 1
    assert bom["serialNumber"].startswith("urn:uuid:")

    serial = uuid.UUID(bom["serialNumber"].removeprefix("urn:uuid:"))
    assert serial.version == 5, "serialNumber 必须是派生 UUIDv5，不得是随机 uuid4"

    meta = bom["metadata"]
    assert meta["timestamp"] == FIXED_TS
    assert meta["tools"]["components"][0]["name"] == sbom.TOOL_NAME
    assert meta["component"]["name"] == sbom.COMPONENT_NAME
    assert meta["component"]["type"] == "application"

    # 落盘文本仍是合法 JSON，且字段一个不少
    payload = json.loads(sbom.render_sbom(bom))
    assert set(payload) == {"bomFormat", "specVersion", "serialNumber", "version", "metadata", "components"}


def test_components_sorted_normalized_and_purl_wellformed(stub_dists):
    stub_dists([("Zeta_Lib", "1.0"), ("Alpha.Lib", "2.0"), ("alpha-lib", "2.0"), ("beta", "0.1")])
    components = sbom.build_sbom(timestamp=FIXED_TS)["components"]

    names = [item["name"] for item in components]
    assert names == sorted(names), "components 必须按 name 排序（顺序确定）"
    assert names == ["alpha-lib", "beta", "zeta-lib"], "PEP 503 规范化后 alpha-lib 应去重为一条"

    for item in components:
        assert item["type"] == "library"
        assert item["purl"] == f"pkg:pypi/{item['name']}@{item['version']}"
        assert item["version"]

    refs = [item["bom-ref"] for item in components]
    assert len(refs) == len(set(refs)), "bom-ref 必须唯一"


def test_empty_distribution_set_still_valid(stub_dists):
    stub_dists([])
    bom = sbom.build_sbom(timestamp=FIXED_TS)

    assert bom["components"] == []
    assert bom["serialNumber"].startswith("urn:uuid:")

    text = sbom.render_sbom(bom)
    payload = json.loads(text)
    assert payload["components"] == []
    assert payload["specVersion"] == "1.5"
    assert text.endswith("\n")


def test_skips_unnamed_and_unversioned_distributions(monkeypatch):
    broken = [
        _StubDist("", "1.0"),
        _StubDist("no-version", None),
        _StubDist("   ", "2.0"),
        _StubDist(sbom.COMPONENT_NAME, sbom.COMPONENT_VERSION),
        _StubDist("ok-lib", "3.0"),
    ]
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: iter(broken))

    bom = sbom.build_sbom(timestamp=FIXED_TS)
    assert [item["name"] for item in bom["components"]] == ["ok-lib"], "空名/无版本/自身发行版都要跳过"
    assert bom["metadata"]["component"]["purl"] == f"pkg:pypi/{sbom.COMPONENT_NAME}@{sbom.COMPONENT_VERSION}"


def test_output_leaks_no_secrets_or_paths(stub_dists, monkeypatch):
    secrets = {
        "HERO_API_KEY": "ak-live-0123456789abcdef",
        "HERO_LLM_API_KEY": "sk-live-0123456789abcdef",
        "HERO_CHECKPOINT_DSN": "postgresql://postgres:pw@localhost:5432/heroquant",
        "HERO_BILLING_DSN": "postgresql://billing:pw@localhost:5432/billing",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    stub_dists([("alpha-lib", "1.0"), ("beta-lib", "2.0")])

    text = sbom.render_sbom(sbom.build_sbom(timestamp=FIXED_TS))
    for key, value in secrets.items():
        assert key not in text
        assert value not in text
    for needle in SENSITIVE_NEEDLES:
        assert needle not in text, f"SBOM 含敏感形态 {needle!r}"


def test_timestamp_defaults_to_source_date_epoch(monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    assert sbom._resolve_timestamp(None) == "2023-11-14T22:13:20Z"

    monkeypatch.setenv("SOURCE_DATE_EPOCH", "not-a-number")
    assert sbom._resolve_timestamp(None).endswith("Z"), "脏 epoch 要回落当前 UTC 而不是崩"


def test_render_is_reproducible_with_epoch_across_processes(monkeypatch, capsys):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: iter([_StubDist("alpha-lib", "1.0")]))

    assert sbom.main(["--output", "-"]) == 0
    first = capsys.readouterr().out
    assert sbom.main(["--output", "-"]) == 0
    second = capsys.readouterr().out
    assert first == second
    assert json.loads(first)["metadata"]["timestamp"] == "2023-11-14T22:13:20Z"


def test_cli_writes_file_with_trailing_newline(stub_dists, tmp_path):
    stub_dists([("alpha-lib", "1.0")])
    out = tmp_path / "sbom.json"

    assert sbom.main(["--output", str(out), "--timestamp", FIXED_TS]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert json.loads(text)["metadata"]["timestamp"] == FIXED_TS


def test_cli_write_failure_returns_one_without_path_echo(stub_dists, tmp_path, capsys):
    stub_dists([("alpha-lib", "1.0")])
    target = tmp_path / "missing-dir" / "sbom.json"

    assert sbom.main(["--output", str(target), "--timestamp", FIXED_TS]) == 1
    captured = capsys.readouterr()
    assert "failed to write SBOM" in captured.err
    assert str(tmp_path) not in captured.err, "stderr 不得回显路径"
    assert captured.out == ""


def test_cli_rejects_bad_timestamp(stub_dists, capsys):
    stub_dists([("alpha-lib", "1.0")])
    assert sbom.main(["--timestamp", "not-a-date"]) == 2
    assert "invalid --timestamp" in capsys.readouterr().err
