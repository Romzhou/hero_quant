# tests/test_skill_disclosure.py
def test_skill_two_phase_disclosure(tmp_path):
    from hero_quant.skills.loader import SkillsLoader
    (tmp_path/"SKILL.md").write_text("---\nname: demo\n---\nbody")
    loader=SkillsLoader([str(tmp_path)])
    desc=loader.get_descriptions()
    assert "demo" in desc
    assert len(desc)<500
    content=loader.get_content("demo")
    assert "body" in content


def test_skill_frontmatter_horizontal_rule_not_truncated(tmp_path):
    from hero_quant.skills.loader import SkillsLoader
    text = "---\nname: demo\n---\nbody line 1\n---\nbody line 2 with --- inside"
    (tmp_path / "SKILL.md").write_text(text, encoding="utf-8")
    loader = SkillsLoader([str(tmp_path)])
    content = loader.get_content("demo")
    assert "body line 2 with --- inside" in content


def test_skill_file_root_and_fallback_md(tmp_path):
    from hero_quant.skills.loader import SkillsLoader
    # single file root
    f = tmp_path / "single.md"
    f.write_text("---\nname: single\n---\nhello single", encoding="utf-8")
    loader = SkillsLoader([str(f)])
    assert "single" in loader.list_skills()
    # fallback *.md when no SKILL.md
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "other.md").write_text("---\nname: other\n---\nworld", encoding="utf-8")
    loader2 = SkillsLoader([str(sub)])
    assert "other" in loader2.list_skills()


def test_skill_lock_exists(tmp_path):
    from hero_quant.skills.loader import SkillsLoader
    import threading
    (tmp_path / "SKILL.md").write_text("---\nname: demo\n---\nbody", encoding="utf-8")
    loader = SkillsLoader([str(tmp_path)])
    assert hasattr(loader, "_lock") and isinstance(loader._lock, type(threading.RLock()))


def test_shadow_account_no_wildcard_leak():
    import hero_quant.shadow.account as acc
    assert not hasattr(acc, "logger")
    assert set(acc.__all__) == {"ShadowRule", "ShadowJournal", "ShadowAccount", "RiskEngine", "DEFAULT_RULES", "ATTRIBUTION_CATEGORIES", "ATTRIBUTION_CN"}
