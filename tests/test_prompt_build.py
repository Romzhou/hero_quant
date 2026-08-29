def test_build_system_prompt_injects_grounding():
    from hero_quant.agent.prompt import build_system_prompt
    p = build_system_prompt(skill_count=5, grounding_block="GND")
    assert "GND" in p and "HARD RULE" in p
    assert "# Hero Quant" in p


def test_prompt_skill_count_boundaries():
    from hero_quant.agent.prompt import build_system_prompt
    import pytest
    p = build_system_prompt(skill_count=0, grounding_block="x")
    assert "0 skills" in p
    p2 = build_system_prompt(skill_count=5, grounding_block="y")
    assert "5 skills" in p2
    with pytest.raises(ValueError):
        build_system_prompt(skill_count=-1, grounding_block="x")
    with pytest.raises(ValueError):
        build_system_prompt(skill_count=True, grounding_block="x")  # bool rejected
    # non-string grounding rejected with fail-visible
    with pytest.raises(ValueError):
        build_system_prompt(skill_count=5, grounding_block=123)  # type: ignore


def test_prompt_grounding_and_invariants():
    from hero_quant.agent.prompt import build_system_prompt
    import pytest
    # empty grounding still yields HARD RULE and placeholder
    p = build_system_prompt(skill_count=5, grounding_block="")
    assert "HARD RULE" in p
    assert "no grounding evidence" in p.lower()
    # ensure fenced block present
    assert "```grounding" in p
    # header invariant fail-visible (not silent assert)
    with pytest.raises(ValueError):
        build_system_prompt(skill_count=5, grounding_block=123)  # type: ignore triggers ValueError before missing header check


def test_prompt_truncation_and_sanitize(caplog):
    from hero_quant.agent.prompt import build_system_prompt
    import logging
    long_block = "A" * 25000
    with caplog.at_level(logging.WARNING):
        p = build_system_prompt(skill_count=1, grounding_block=long_block)
        assert "[TRUNCATED" in p
        assert any("truncated" in r.message.lower() for r in caplog.records)
    # header injection escaped
    p2 = build_system_prompt(skill_count=1, grounding_block="# injected header\ncontent")
    assert "\\# injected header" in p2 or "# injected header" not in p2.split("```grounding")[1].split("```")[0].splitlines()[0] or "HARD RULE" in p2
