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
