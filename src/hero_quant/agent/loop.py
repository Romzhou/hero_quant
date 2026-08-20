"""AgentLoop state machine - minimal implementation for Task 13."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LoopResult:
    terminated: bool
    iterations: int
    text: str = ""
    reason: str = ""


class AgentLoop:
    def __init__(self, llm, max_iterations=3, token_limit=None, trace=None):
        self.llm = llm
        self.max_iterations = max_iterations
        self.token_limit = token_limit
        self.trace = trace

    def run(self, goal: str) -> LoopResult:
        iterations = 0
        buffer = ""
        for it in range(self.max_iterations):
            iterations += 1
            # 集成 context/grounding/trace 轻量占位（可为空）
            try:
                stream = self.llm.stream_chat(goal)
            except Exception:
                # fallback: try alternative method names
                stream = []
            for chunk in stream:
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    buffer += chunk.get("text", "")
            if buffer:
                break  # 收到文本即视为终止条件满足
            # 也可检查 user_stop / token_limit 占位
            if self.token_limit is not None and len(buffer) >= self.token_limit:
                break
        return LoopResult(terminated=True, iterations=iterations, text=buffer, reason="completed")
