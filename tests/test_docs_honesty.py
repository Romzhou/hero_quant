"""Docs honesty — Wave5 Task15（去掉恒真 or 分支，锚定具体文档行）."""
import pathlib


def test_hero_data_mode_docs_aligns_live():
    readme = pathlib.Path("README.md").read_text(encoding="utf-8")
    from hero_quant.config.settings import Settings

    s = Settings()
    assert s.data_mode == "live", f"Settings.data_mode should be live, got {s.data_mode}"
    assert "HERO_DATA_MODE=live" in readme, "README should document HERO_DATA_MODE=live"
    assert "| `HERO_DATA_MODE` | `live`" in readme, "README table default should be live"


def test_checkpoint_pg_not_memory():
    readme = pathlib.Path("README.md").read_text(encoding="utf-8")
    from hero_quant.config.settings import Settings

    s = Settings()
    assert s.checkpoint_dsn.startswith("postgresql://"), f"checkpoint_dsn should be PG, got {s.checkpoint_dsn}"
    # 具体到 env 表行：必须给出 PG DSN 形态，而不是任意位置出现 "PG"
    assert "| `HERO_CHECKPOINT_DSN` | `postgresql://" in readme, "README env 表必须给出 checkpoint PG DSN"
    assert "当前 `memory://` 可跑" not in readme, "README still claims memory:// primary, should be PG fallback memory"


def test_hash_lock_not_placeholder():
    readme = pathlib.Path("README.md").read_text(encoding="utf-8")
    lock = pathlib.Path("requirements-lock.txt").read_text(encoding="utf-8")
    assert "--hash=sha256:" in lock, "requirements-lock should contain real hash"
    assert "--hash=sha256:placeholder" not in lock.lower()
    assert lock.count("--hash=sha256:") > 20, "should have many real hashes"
    assert "真 sha256" in readme or "真 hash" in readme, "README should mention real hashes"
    lock_lines = [ln for ln in readme.splitlines() if "requirements-lock.txt" in ln]
    assert lock_lines, "README 必须有一行说明 requirements-lock.txt"
    for line in lock_lines:
        if "占位" in line:
            assert "非占位" in line or "真" in line, f"README line still says bare 占位: {line}"


def test_changelog_has_030_2026_08_28():
    changelog = pathlib.Path("CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.3.0] - 2026-08-28" in changelog, "CHANGELOG missing 0.3.0 2026-08-28 entry"
    assert "Wave5" in changelog or "Supplement" in changelog
    assert any(sha in changelog for sha in ("7559422", "f639a78", "e79dfc2")), "CHANGELOG 缺少对应提交号"


def test_retrieval_eval_has_ablation():
    doc = pathlib.Path("docs/retrieval_eval.md").read_text(encoding="utf-8")
    assert "ablation" in doc.lower()
    # 锚定三行具体表格，而不是文档任意位置出现 "32"/"128"/"768"
    for row in ("| 32 |", "| 128 |", "| 768 |"):
        assert row in doc, f"retrieval_eval 缺少维度 ablation 行：{row}"
    assert "dim=32" in doc and "dim=128" in doc and "dim=768" in doc
