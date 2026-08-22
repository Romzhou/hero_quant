"""Agent 内核包：Loop/上下文/证据/轨迹/图的轻量编排核心。

职责：以最小内核组织量化投研 Agent 的执行、记忆、校验与审计能力。
架构位置：hero_quant 顶层 agent 域，聚合 trace/context/grounding/prompt/graph/state 等子模块。
关键设计：Loop 驱动迭代，Context 负责折叠，Grounding 提供事实源，Trace 侧车保可重放，Graph 承载并行研究团队。
"""

from .trace import TraceWriter

__all__ = ["TraceWriter"]
