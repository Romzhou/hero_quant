"""System Prompt 组装与 Grounding 三级校验注入。

职责：以固定四段式组装面向 LLM 的系统提示词，并将 Ground Truth 证据注入可审计位置。
架构位置：agent 层 prompt 底座，被 ContextManager/Loop 调用，依赖 GroundingLedger 的证据块。
关键设计：
- 四段结构：Output Principles / Tool-Skill / Grounding / HARD RULE，保证审计与测试可校验
- 三级校验：L1 ingest 建证据 → L2 assert 阻断幻觉 → L3 prompt 注入限定引用
- 防御性保证：即使无证据也保留占位，HARD RULE 与 grounding_block 必出现于终稿
"""

from __future__ import annotations


# 段模板：保持简洁明确，便于测试与审计校验

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
    """组装含 Grounding 三级校验的 System Prompt，未提供证据时使用占位块."""
    block = grounding_block
    if not block and ledger is not None:
        try:
            block = ledger.render_block()  # type: ignore[union-attr]
        except Exception:
            block = ""
    if not block:
        block = "(no grounding evidence yet — any price quote must be blocked)"

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

    # 防御性：确保关键不变量必出现，便于审计
    if block and block not in prompt:
        prompt += f"\n{block}\n"
    if "HARD RULE" not in prompt:
        prompt += "\n" + HARD_RULE

    return prompt


def build_prompt(*args, **kwargs) -> str:
    """build_system_prompt 的兼容别名."""
    return build_system_prompt(*args, **kwargs)


__all__ = ["build_system_prompt", "build_prompt"]
