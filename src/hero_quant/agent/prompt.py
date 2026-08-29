"""System Prompt 组装与 Grounding 三级校验注入。

职责：以固定四段式组装面向 LLM 的系统提示词，并将 Ground Truth 证据注入可审计位置。
架构位置：agent 层 prompt 底座，被 ContextManager/Loop 调用，依赖 GroundingLedger 的证据块。
关键设计：
- 四段结构：Output Principles / Tool-Skill / Grounding / HARD RULE，保证审计与测试可校验
- 三级校验：L1 ingest 建证据 → L2 assert 阻断幻觉 → L3 prompt 注入限定引用
- 防御性保证：即使无证据也保留占位，HARD RULE 与 grounding_block 必出现于终稿
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

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

_MAX_UNTRUSTED_LEN = 20000


def _sanitize_untrusted(text: str, field: str = "block") -> str:
    """Escape markdown headers and fence terminators, enforce length limit."""
    if not isinstance(text, str):
        raise ValueError(f"{field} must be str, got {type(text).__name__}")
    if len(text) > _MAX_UNTRUSTED_LEN:
        logger.warning("prompt %s truncated to %d (was %d)", field, _MAX_UNTRUSTED_LEN, len(text))
        text = text[:_MAX_UNTRUSTED_LEN] + "\n[TRUNCATED: exceeds max length]"
    # Escape fence terminator to prevent breakout
    text = text.replace("```", "`\\``")
    # Escape HTML-sensitive chars to prevent injection when prompt rendered in HTML contexts
    # Preserve existing fence/header escaping above; html.escape covers < > &
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Avoid double-escaping the header escape: restore "\#" if html mangled it (it doesn't, but be explicit)
    # Note: html.escape was avoided via manual replace to keep "\" escapes intact
    # Escape leading markdown headers line by line
    lines = text.splitlines()
    escaped: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#"):
            idx = line.find("#")
            escaped.append(line[:idx] + "\\" + line[idx:])
        else:
            escaped.append(line)
    return "\n".join(escaped)


def _fenced_block(content: str, label: str = "text") -> str:
    """Wrap sanitized content in a fenced block for isolation."""
    return f"```{label}\n{content}\n```"


def build_system_prompt(
    skill_count: int = 5,
    grounding_block: str = "",
    *,
    ledger=None,
    skills_digest: str = "",
    extra_rules: str = "",
) -> str:
    """组装含 Grounding 三级校验的 System Prompt，未提供证据时使用占位块."""
    # --- validate skill_count explicitly before int() ---
    if isinstance(skill_count, bool):
        raise ValueError("skill_count must be int, got bool")
    try:
        skill_count_int = int(skill_count)  # type: ignore[arg-type]
    except (ValueError, TypeError) as exc:
        raise ValueError(f"skill_count must be int, got {skill_count!r}") from exc
    if skill_count_int < 0 or skill_count_int > 10000:
        raise ValueError(f"skill_count out of range: {skill_count_int}")

    # --- validate types for untrusted blocks ---
    for name, val in [
        ("grounding_block", grounding_block),
        ("skills_digest", skills_digest),
        ("extra_rules", extra_rules),
    ]:
        if val is not None and not isinstance(val, str):
            raise ValueError(f"{name} must be str, got {type(val).__name__}")

    block = grounding_block
    if not block and ledger is not None:
        try:
            block = ledger.render_block()  # type: ignore[union-attr]
        except (AttributeError, ValueError, TypeError, RuntimeError) as exc:
            logger.exception("ledger.render_block failed: %s", exc)
            block = ""
        except Exception as exc:  # narrow fallback but still log
            logger.exception("unexpected ledger.render_block failure: %s", exc)
            block = ""
    if not block:
        block = "(no grounding evidence yet — any price quote must be blocked)"

    if not isinstance(block, str):
        raise ValueError(f"grounding_block must be str, got {type(block).__name__}")

    # Sanitize untrusted inputs and wrap in fenced blocks for isolation
    safe_block = _sanitize_untrusted(block, "grounding_block")
    safe_digest = _sanitize_untrusted(skills_digest, "skills_digest") if skills_digest else ""
    safe_extra = _sanitize_untrusted(extra_rules, "extra_rules") if extra_rules else ""

    fenced_grounding = _fenced_block(safe_block, "grounding")
    tool_skill = TOOL_SKILL_TEMPLATE.format(skill_count=skill_count_int)
    if safe_digest:
        tool_skill += f"\nSkills digest:\n{_fenced_block(safe_digest, 'skills')}\n"

    grounding_section = GROUNDING_TEMPLATE.format(grounding_block=fenced_grounding)

    parts: list[str] = [
        HEADER,
        OUTPUT_PRINCIPLES,
        tool_skill,
        grounding_section,
    ]
    if safe_extra:
        parts.append(f"## Extra Rules\n{_fenced_block(safe_extra, 'extra')}\n")
    parts.append(HARD_RULE)
    parts.append(FOOTER)

    prompt = "\n".join(parts)

    # Deterministic invariants: fail-visible explicit checks (assert stripped under -O)
    if "HARD RULE" not in prompt:
        logger.error("HARD_RULE invariant broken")
        raise ValueError("HARD_RULE invariant broken")
    if HEADER not in prompt:
        logger.error("HEADER missing from prompt")
        raise ValueError("HEADER missing")
    if fenced_grounding not in prompt:
        logger.error("grounding block missing from prompt")
        raise ValueError("grounding block missing")

    return prompt


def build_prompt(*args, **kwargs) -> str:
    """build_system_prompt 的兼容别名."""
    return build_system_prompt(*args, **kwargs)


__all__ = ["build_system_prompt", "build_prompt"]
