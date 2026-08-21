def test_build_system_prompt_injects_grounding():
    from hero_quant.agent.prompt import build_system_prompt
    p = build_system_prompt(skill_count=5, grounding_block="GND")
    assert "GND" in p and "HARD RULE" in p
