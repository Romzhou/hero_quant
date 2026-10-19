"""K线 G-14 回归锁：.env 永不入库 + DSN 脱敏覆盖空用户名形态。

中文：只读断言，不读取 .env 真值（AGENTS §2）；git 历史面干净由审计 G-14 背书，
此处锁住三件事：.gitignore 覆盖 .env、settings 正典脱敏同时覆盖 user:pass@ 与
:pass@（空用户名）两种形态、server 薄委托与正典一致。
"""
from __future__ import annotations

import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_k_g14_gitignore_covers_env():
    lines = (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in [ln.strip() for ln in lines], ".gitignore 必须覆盖 .env"


def test_k_g14_redact_covers_both_userinfo_forms():
    from hero_quant.config.settings import _redact_dsn

    covered = _redact_dsn("postgresql://alice:s3cret@db:5432/app")
    assert "s3cret" not in covered and "***" in covered
    empty_user = _redact_dsn("postgresql://:s3cret@db:5432/app")
    assert "s3cret" not in empty_user and "***" in empty_user


def test_k_g14_server_delegates_to_canonical():
    from hero_quant.api.server import _redact_dsn_api
    from hero_quant.config.settings import _redact_dsn

    for dsn in (
        "postgresql://alice:s3cret@db:5432/app",
        "postgresql://:s3cret@db:5432/app",
    ):
        assert _redact_dsn_api(dsn) == _redact_dsn(dsn), "server 脱敏不得与正典分叉"
