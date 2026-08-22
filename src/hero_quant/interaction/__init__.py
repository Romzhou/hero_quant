"""interaction 包 —— 人机问答与审批中断。

职责：暴露问询卡片、UserQuestionService 与审批服务；架构位置：interaction 层衔接图执行与前端。
设计决策：两段式 ask→guard，先校验意图再委托 provider；Store 按 (tenant, thread) 隔离。
"""

from .questions import AskCardInterrupt, AskUserQuestionItem, Command, UserQuestionService
from .approval import ApprovalPolicy, ApprovalService

__all__ = [
    "AskCardInterrupt",
    "AskUserQuestionItem",
    "Command",
    "UserQuestionService",
    "ApprovalPolicy",
    "ApprovalService",
]
