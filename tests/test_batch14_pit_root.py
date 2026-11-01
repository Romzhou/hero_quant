"""批次14 决策：HERO_PIT_ROOT 的 env 网关与三处联动。

- 默认 /root/hero-data/pit（仓库外）；
- 必须是绝对路径且不含波浪号（expanduser 在 systemd/容器里语义不同），否则 ValueError；
- 三处联动：settings.py + .env.example + README env 表（AGENTS §2）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hero_quant.config.settings import Settings

_REPO = Path(__file__).resolve().parents[1]


def test_default_is_outside_repo(monkeypatch):
    monkeypatch.delenv("HERO_PIT_ROOT", raising=False)
    root = Settings().pit_root
    assert root == "/root/hero-data/pit"
    assert not root.startswith(str(_REPO))


def test_absolute_path_accepted_and_normalized(monkeypatch):
    monkeypatch.setenv("HERO_PIT_ROOT", "/data/pit/")
    assert Settings().pit_root == "/data/pit"


@pytest.mark.parametrize("bad", ["rel/pit", "~/pit", "~root/pit"])
def test_non_absolute_or_tilde_rejected(monkeypatch, bad):
    monkeypatch.setenv("HERO_PIT_ROOT", bad)
    with pytest.raises(ValueError, match="HERO_PIT_ROOT"):
        Settings()


def test_three_way_documentation(monkeypatch):
    env_example = (_REPO / ".env.example").read_text(encoding="utf-8")
    readme = (_REPO / "README.md").read_text(encoding="utf-8")
    assert "HERO_PIT_ROOT=/root/hero-data/pit" in env_example
    assert "| `HERO_PIT_ROOT` | `/root/hero-data/pit`" in readme
