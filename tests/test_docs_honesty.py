"""Docs honesty — Wave5 Task15 TDD 5 asserts."""
import pathlib


def test_hero_data_mode_docs_aligns_live():
    readme = pathlib.Path("README.md").read_text(encoding="utf-8")
    from hero_quant.config.settings import Settings

    s = Settings()
    # Settings default must be live (production safe)
    assert s.data_mode == "live", f"Settings.data_mode should be live, got {s.data_mode}"
    # README docs should mention live as default and example
    assert "HERO_DATA_MODE=live" in readme, "README should document HERO_DATA_MODE=live"
    # Table default should be live, not synthetic placeholder
    assert "| `HERO_DATA_MODE` | `live`" in readme, "README table default should be live"


def test_checkpoint_pg_not_memory():
    readme = pathlib.Path("README.md").read_text(encoding="utf-8")
    from hero_quant.config.settings import Settings

    s = Settings()
    # Settings checkpoint_dsn default is PG, not memory://
    assert s.checkpoint_dsn.startswith("postgresql://"), f"checkpoint_dsn should be PG, got {s.checkpoint_dsn}"
    # README should not claim current is memory:// as primary; should mention PG with fallback
    assert "postgresql://postgres:postgres@localhost:5432/hero_quant" in readme or "PG" in readme
    # Ensure the old misleading line "当前 `memory://` 可跑" as sole description is gone
    assert "当前 `memory://` 可跑" not in readme, "README still claims memory:// primary, should be PG fallback memory"


def test_hash_lock_not_placeholder():
    readme = pathlib.Path("README.md").read_text(encoding="utf-8")
    lock = pathlib.Path("requirements-lock.txt").read_text(encoding="utf-8")
    # requirements-lock should contain real sha256 hashes, not placeholder hashes
    assert "--hash=sha256:" in lock, "requirements-lock should contain real hash"
    # Allow comment mentioning "not placeholder" but reject actual placeholder hash values
    assert "--hash=sha256:placeholder" not in lock.lower()
    assert lock.count("--hash=sha256:") > 20, "should have many real hashes"
    # README should describe real hashes, not 占位
    assert "真 sha256" in readme or "真 hash" in readme, "README should mention real hashes"
    # Old placeholder phrase should be gone from that line context
    # The line describing requirements-lock should not say 占位 (except "非占位" is ok)
    for line in readme.splitlines():
        if "requirements-lock.txt" in line:
            # allow "非占位" but not bare 占位 placeholder claim
            # Check that line does not contain "占位" without "非占位" or "真"
            if "占位" in line:
                assert "非占位" in line or "真" in line, f"README line still says bare 占位: {line}"


def test_changelog_has_030_2026_08_28():
    changelog = pathlib.Path("CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.3.0] - 2026-08-28" in changelog, "CHANGELOG missing 0.3.0 2026-08-28 entry"
    assert "Wave5" in changelog or "Supplement" in changelog
    assert "7559422" in changelog or "f639a78" in changelog or "e79dfc2" in changelog


def test_retrieval_eval_has_ablation():
    doc = pathlib.Path("docs/retrieval_eval.md").read_text(encoding="utf-8")
    # Should contain ablation table with dimensions 32/128/768
    assert "ablation" in doc.lower()
    assert "32" in doc and "128" in doc and "768" in doc, "retrieval_eval missing 32/128/768 dimension ablation"
    # Table row check
    assert "| 32" in doc or "|32" in doc
    assert "| 128" in doc or "|128" in doc
    assert "| 768" in doc or "|768" in doc
