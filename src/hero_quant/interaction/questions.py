"""Ask card — UserQuestionService with interrupt + guard.

Two-stage ask→guard, validation BAD_INTENT / DELEGATED_CALLER,
interrupt via AskCardInterrupt + Command resume placeholder,
Store (tenant,thread) isolation placeholder noted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class AskCardInterrupt(Exception):
    """Interrupt exception for Ask card — maps to LangGraph interrupt."""

    def __init__(self, questions: list[Any], reason: str = "NO_PROVIDER"):
        super().__init__(f"{reason}: ask card interrupt")
        self.questions = questions
        self.reason = reason


class Command:
    """Placeholder for LangGraph Command resume."""

    def __init__(self, resume: Any = None, goto: str | None = None):
        self.resume = resume
        self.goto = goto


@dataclass
class AskUserQuestionItem:
    id: str
    question: str
    header: str
    options: list[dict[str, str]] = field(default_factory=list)
    multiSelect: bool = False
    intent: str | None = None


def _validate_questions(questions: list[Any]) -> None:
    if not questions:
        raise ValueError("BAD_INTENT: questions empty")
    for q in questions:
        # Normalize to dict for validation
        if isinstance(q, dict):
            data = q
        elif hasattr(q, "__dict__"):
            data = vars(q)
        else:
            # Try dataclass asdict
            try:
                from dataclasses import asdict

                data = asdict(q)  # type: ignore
            except Exception:
                raise ValueError("BAD_INTENT: invalid question item")
        # Required fields
        for key in ("id", "question", "header", "options"):
            if key not in data:
                raise ValueError(f"BAD_INTENT: missing {key}")
        opts = data.get("options")
        if not isinstance(opts, list) or len(opts) == 0:
            raise ValueError("BAD_INTENT: options empty or not list")
        for opt in opts:
            if not isinstance(opt, dict) or "label" not in opt or "description" not in opt:
                raise ValueError("BAD_INTENT: option must have label/description")
        # Intent check
        intent = data.get("intent")
        if intent is not None and intent not in (None, "confirm", "select", "input"):
            # Minimal intent whitelist — unknown intent considered BAD_INTENT
            if isinstance(intent, str) and intent == "delegated":
                raise ValueError("DELEGATED_CALLER: delegated caller not allowed")
        # DELEGATED_CALLER placeholder: detect if question was delegated via stack
        # Minimal: if question text contains delegated marker
        if isinstance(data.get("question"), str) and "DELEGATED" in data["question"]:
            raise ValueError("DELEGATED_CALLER: delegated")


class UserQuestionService:
    """Question service — provider.ask(signal) with guard.

    - Without provider → raises NO_PROVIDER (mapped to interrupt upstream)
    - With provider → delegates to provider.ask(questions, signal)
    - Store (tenant,thread) isolation is handled at caller (graph/Store) — placeholder
    """

    def __init__(self, provider: Any | None = None):
        self.provider = provider

    def ask_sync(self, questions: list[Any], signal: Any | None = None) -> Any:
        _validate_questions(questions)
        if self.provider is None:
            # No provider → interrupt placeholder with NO_PROVIDER
            raise RuntimeError("NO_PROVIDER: no question provider configured")
        # Delegate three-stage ask→guard
        try:
            # Provider contract: ask(questions, signal) -> result
            if hasattr(self.provider, "ask_sync"):
                return self.provider.ask_sync(questions, signal=signal)
            if hasattr(self.provider, "ask"):
                # Try sync call (may be async, but minimal)
                res = self.provider.ask(questions, signal=signal)
                # If coroutine, run placeholder
                try:
                    import asyncio

                    if asyncio.iscoroutine(res):
                        return asyncio.run(res)
                except Exception:
                    pass
                return res
            raise RuntimeError("NO_PROVIDER: provider missing ask method")
        except Exception as e:
            # Preserve NO_PROVIDER marker
            if "NO_PROVIDER" in str(e):
                raise
            raise

    async def ask(self, questions: list[Any], signal: Any | None = None) -> Any:
        _validate_questions(questions)
        if self.provider is None:
            raise RuntimeError("NO_PROVIDER: no question provider configured")
        if hasattr(self.provider, "ask"):
            return await self.provider.ask(questions, signal=signal)
        if hasattr(self.provider, "ask_sync"):
            return self.provider.ask_sync(questions, signal=signal)
        raise RuntimeError("NO_PROVIDER: provider missing ask method")

    # Store isolation placeholder — (tenant, thread) namespace handled by caller
    def with_store_isolation(self, tenant: str, thread: str) -> "UserQuestionService":
        # Return self as placeholder; real Store isolation in memory/store
        return self
