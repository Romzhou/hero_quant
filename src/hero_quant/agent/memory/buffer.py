"""Agent 对话记忆缓冲：deque 自动裁剪。

职责：承载单会话最近 max_turns 轮对话（user+assistant 计 2 条/轮），供
AgentLoop 经 duck-typing 注入/写回时复用。
关键设计：collections.deque(maxlen=max_turns*2) O(1) 自动裁剪替代 O(n) 列表
切片；溢出时按轮边界裁剪保证窗口头为 user；系统消息单独保存，默认上限
DEFAULT_SYSTEM_LIMIT 条（system_limit 可调），淘汰时显式 warning 不静默。
"""

from __future__ import annotations

import html
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# 中文：system 消息默认上限。有界是为避免逐轮注入（skills_digest、memory 片段等）
# 无限累积撑爆 LLM 上下文；可调 + 淘汰告警是为不静默丢弃上下文（G2#4）。
DEFAULT_SYSTEM_LIMIT = 10


@dataclass
class Message:
    """轻量消息元素：role/content + to_dict，与 loop 归一化兼容。"""

    role: str
    content: str

    def to_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


class MemoryBuffer:
    """对话记忆缓冲区（基于 deque 实现 O(1) 自动裁剪）。"""

    def __init__(self, max_turns: int = 20, system_limit: int | None = None):
        # 中文：校验 max_turns 避免 0/负数 丢全部或抛裸 ValueError
        if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns <= 0:
            raise ValueError(f"max_turns must be a positive int, got {max_turns!r}")
        self.max_turns = max_turns
        self._capacity = max_turns * 2
        self._messages: deque = deque(maxlen=self._capacity)
        # 中文：系统消息有界（默认 10 条），避免逐轮累积撑爆 LLM 上下文；
        # 上限经 system_limit 可调，淘汰时告警（见 add_system_message）。
        self._system_limit = DEFAULT_SYSTEM_LIMIT if system_limit is None else system_limit
        if (
            not isinstance(self._system_limit, int)
            or isinstance(self._system_limit, bool)
            or self._system_limit <= 0
        ):
            raise ValueError(f"system_limit must be a positive int, got {system_limit!r}")
        self._system_messages: deque = deque(maxlen=self._system_limit)

    def _append(self, msg: Message):
        """追加普通消息；溢出时按轮边界裁剪，保证窗口头为 user。"""
        at_cap = len(self._messages) >= self._capacity
        self._messages.append(msg)
        if at_cap:
            self._align_head()

    def _align_head(self):
        """逐出头部孤儿 assistant/tool 消息直到 head 为 user 或为空。"""
        while self._messages and self._messages[0].role != "user":
            self._messages.popleft()

    def add_user_message(self, content: str):
        """添加用户消息"""
        self._append(Message(role="user", content=content))

    def add_assistant_message(self, content: str):
        """添加助手消息"""
        self._append(Message(role="assistant", content=content))

    def add_tool_result(self, tool_name: str, result: str):
        """添加工具结果"""
        # 中文：转义 XML 注入，防闭合标签破坏下游解析
        safe_name = html.escape(tool_name, quote=True)
        safe_result = html.escape(result)
        self._append(
            Message(role="tool", content=f'<tool_result name="{safe_name}">{safe_result}</tool_result>')
        )

    def add_system_message(self, content: str):
        """添加系统消息（不受 max_turns 限制，受 system_limit 限制，淘汰时告警）。"""
        if len(self._system_messages) >= self._system_limit:
            # 中文：淘汰必须可见——静默丢弃会让 Agent 在不知情下丢失指令/约束（G2#4）
            logger.warning(
                "system message limit reached (%d): evicting oldest system message",
                self._system_limit,
            )
        self._system_messages.append(Message(role="system", content=content))

    def get_messages(self) -> List[Message]:
        """获取消息列表（系统消息 + 普通消息）"""
        return list(self._system_messages) + list(self._messages)

    def get_messages_for_llm(self) -> List[dict]:
        """获取给 LLM 的消息格式"""
        return [m.to_dict() for m in self.get_messages()]

    @property
    def messages(self) -> List[Message]:
        """兼容旧接口：返回消息列表"""
        return self.get_messages()

    @messages.setter
    def messages(self, value: List[Message]):
        """兼容旧接口：设置消息列表；超容直接抛 ValueError（last-N 需调用方显式裁剪）"""
        staged_system: list = []
        staged: list = []
        for m in value:
            if isinstance(m, dict):
                try:
                    m = Message(m["role"], m["content"])
                except (KeyError, TypeError) as e:
                    raise ValueError(f"invalid message entry: {m!r}") from e
            if not isinstance(m, Message):
                raise ValueError(f"invalid message entry: {m!r}")
            (staged_system if m.role == "system" else staged).append(m)
        if len(staged) > self.max_turns * 2:
            raise ValueError(f"messages exceed capacity: {len(staged)} > {self.max_turns * 2}")
        if len(staged_system) > self._system_limit:
            raise ValueError(f"system messages exceed capacity: {len(staged_system)} > {self._system_limit}")
        self._system_messages.clear()
        self._messages.clear()
        self._system_messages.extend(staged_system)
        self._messages.extend(staged)
        self._align_head()

    def clear(self):
        """清空记忆"""
        self._messages.clear()
        self._system_messages.clear()

    def to_dict(self) -> Dict[str, Any]:
        """序列化"""
        return {"messages": [m.to_dict() for m in self.get_messages()], "max_turns": self.max_turns}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryBuffer":
        """反序列化"""
        # 中文：校验输入形态，fail-visible
        if not isinstance(data, dict):
            raise ValueError(f"invalid buffer data: {type(data).__name__}")
        buffer = cls(max_turns=data.get("max_turns", 20))
        msgs = data.get("messages", [])
        if msgs is None:
            raise ValueError("invalid buffer data: messages is None")
        if not isinstance(msgs, list):
            raise ValueError(f"invalid buffer data: messages must be list, got {type(msgs).__name__}")
        staged_system: list = []
        staged: list = []
        for m in msgs:
            try:
                role, content = m["role"], m["content"]
            except (KeyError, TypeError) as e:
                raise ValueError(f"invalid message entry: {m!r}") from e
            msg = Message(role, content)
            (staged_system if msg.role == "system" else staged).append(msg)
        if len(staged) > buffer._capacity:
            raise ValueError(f"messages exceed capacity: {len(staged)} > {buffer._capacity}")
        if len(staged_system) > buffer._system_limit:
            raise ValueError(f"system messages exceed capacity: {len(staged_system)} > {buffer._system_limit}")
        buffer._system_messages.extend(staged_system)
        buffer._messages.extend(staged)
        buffer._align_head()
        return buffer
