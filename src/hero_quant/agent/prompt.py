"""BuildSystemPrompt + Grounding 三级校验入 prompt.

260 行简化版，保留 4 大段：
  1. Output Principles — 输出规范
  2. Tool / Skill      — 工具与技能声明
  3. Grounding         — Ground Truth 注入（三级校验载体）
  4. HARD RULE         — 不可违背硬规则

Grounding 三级校验：
  L1 ingest  — ledger.ingest(symbol, bars) 建证据
  L2 assert  — ledger.assert_price(symbol, price) 阻断幻觉
  L3 prompt  — render_block() 注入 system prompt，LLM 只能引用证据内价格

Usage:
    from hero_quant.agent.grounding import GroundingLedger
    from hero_quant.agent.prompt import build_system_prompt

    ledger = GroundingLedger()
    ledger.ingest("600519.SH", [{"close": 1500.0, "date": "2026-08-19"}])
    prompt = build_system_prompt(skill_count=5, grounding_block=ledger.render_block())
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Section templates — kept concise but explicit for tests & audit
# ---------------------------------------------------------------------------

OUTPUT_PRINCIPLES = """## Output Principles
- Be concise, precise, and actionable.
- Numbers must be grounded in evidence; never hallucinate prices.
- Prefer structured output (tables/markdown) when presenting data.
- Cite sources: every price/indicator must trace to a tool result or Ground Truth.
- No forward-looking guarantee; disclose assumptions and costs.
"""

TOOL_SKILL_TEMPLATE = """## Tool / Skill
- Available tools are read-only by default; write tools require explicit confirmation.
- Skills are loaded on demand: {skill_count} skills available via digest → full <skill_content>.
- Use vector router TopK selection; call at most 5 tools per turn.
- is_concurrency_safe=True tools may run in parallel pool; write tools run serially.
- All tool calls are traced via TraceWriter and redacted via redact_payload.
"""

GROUNDING_TEMPLATE = """## Grounding — Evidence Only
{grounding_block}
- L1 ingest: evidence comes from MarketDataRegistry / bars only.
- L2 assert: any price mention must pass GroundingLedger.assert_price(symbol, price).
- L3 prompt: this Ground Truth block is the only price source the model may quote.
- If a price is not in evidence, respond with "not in evidence" and do not invent.
"""

HARD_RULE = """## HARD RULE
- HARD RULE: Never quote a price that is not in Ground Truth evidence.
- HARD RULE: Cross-source deviation >1% must block, not warn (CrossSourceError).
- HARD RULE: Weights timestamp must be >= price date (PIT); future weights -> ValidationError.
- HARD RULE: All mutations via ledger/governance must be verifiable (ledger.verify()).
- HARD RULE: Tool output containing price must be grounding-verified before final answer.
"""

HEADER = """# Hero Quant — System Prompt
You are Hero Quant, a production-grade quant research assistant.
Kernel: Loop + Context + Grounding + Trace. Follow the rules below strictly.
"""

FOOTER = """---
Operational Notes:
- ContextManager max_chars folding: head2 + [SUMMARY] + tail2, banner TRUNCATED.
- Token limit 0.8 triggers compact; token_limit exceeded -> TRUNCATED banner + budget_fallback.
- RetryPolicy with exponential backoff for transient LLM errors; max_iterations guards loops.
"""


def build_system_prompt(
    skill_count: int = 5,
    grounding_block: str = "",
    *,
    ledger=None,
    skills_digest: str = "",
    extra_rules: str = "",
) -> str:
    """Build system prompt with Grounding 三级校验 injection.

    Args:
        skill_count: number of skills available (injected into Tool/Skill section).
        grounding_block: pre-rendered Ground Truth block (string). If empty and
            ledger is provided, ledger.render_block() is used.
        ledger: optional GroundingLedger instance — if given and grounding_block
            is empty, render_block() is called automatically.
        skills_digest: optional short skills digest to append under Tool/Skill.
        extra_rules: optional extra rules appended before HARD RULE.

    Returns:
        Full system prompt string containing GND injection and HARD RULE.
    """
    # Resolve grounding block: explicit string wins, else ledger.render_block()
    block = grounding_block
    if not block and ledger is not None:
        try:
            block = ledger.render_block()  # type: ignore[union-attr]
        except Exception:
            block = ""
    # Fallback placeholder keeps Grounding section valid even when empty
    if not block:
        block = "(no grounding evidence yet — any price quote must be blocked)"

    # Ensure block is string
    if not isinstance(block, str):
        block = str(block)

    tool_skill = TOOL_SKILL_TEMPLATE.format(skill_count=int(skill_count))
    if skills_digest:
        tool_skill += f"\nSkills digest:\n{skills_digest}\n"

    grounding_section = GROUNDING_TEMPLATE.format(grounding_block=block)

    parts = [
        HEADER,
        OUTPUT_PRINCIPLES,
        tool_skill,
        grounding_section,
    ]
    if extra_rules:
        parts.append(f"## Extra Rules\n{extra_rules}\n")
    parts.append(HARD_RULE)
    parts.append(FOOTER)

    prompt = "\n".join(parts)

    # Defensive: guarantee invariants for tests & audits
    # 1) grounding_block substring must appear verbatim when provided
    if block and block not in prompt:
        # fallback injection if templating failed (should not happen)
        prompt += f"\n{block}\n"
    # 2) HARD RULE must appear
    if "HARD RULE" not in prompt:
        prompt += "\n" + HARD_RULE

    return prompt


# Backwards compat alias — some callers expect build_prompt
def build_prompt(*args, **kwargs) -> str:
    return build_system_prompt(*args, **kwargs)


__all__ = ["build_system_prompt", "build_prompt"]
