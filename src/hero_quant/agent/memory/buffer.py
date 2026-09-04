"""Agent 对话记忆缓冲：deque 自动裁剪。

职责：承载单会话最近 max_turns 轮对话（user+assistant 计 2 条/轮），供
AgentLoop 经 duck-typing 注入/写回时复用。
关键设计：collections.deque(maxlen=max_turns*2) O(1) 自动裁剪替代 O(n) 列表
切片；系统消息单独保存不受裁剪影响。
"""

from __future__ import annotations

import html
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class Message:
    """轻量消息元素：role/content + to_dict，与 loop 归一化兼容。"""

    role: str
    content: str

    def to_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


class MemoryBuffer:
    """对话记忆缓冲区（基于 deque 实现 O(1) 自动裁剪）。"""

    def __init__(self, max_turns: int = 20):
        # 中文：校验 max_turns 避免 0/负数 丢全部或抛裸 ValueError
        if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns <= 0:
            raise ValueError(f"max_turns must be a positive int, got {max_turns!r}")
        self.max_turns = max_turns
        self._messages: deque = deque(maxlen=max_turns * 2)
        # 中文：系统消息不受裁剪影响，无界 deque
        self._system_messages: deque = deque()

    def add_user_message(self, content: str):
        """添加用户消息"""
        self._messages.append(Message(role="user", content=content))

    def add_assistant_message(self, content: str):
        """添加助手消息"""
        self._messages.append(Message(role="assistant", content=content))

    def add_tool_result(self, tool_name: str, result: str):
        """添加工具结果"""
        # 中文：转义 XML 注入，防闭合标签破坏下游解析
        safe_name = html.escape(tool_name, quote=True)
        safe_result = html.escape(result)
        self._messages.append(
            Message(role="tool", content=f'<tool_result name="{safe_name}">{safe_result}</tool_result>')
        )

    def add_system_message(self, content: str):
        """添加系统消息（不受 max_turns 限制）"""
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
        """兼容旧接口：设置消息列表"""
        self._system_messages.clear()
        self._messages.clear()
        for m in value:
            if m.role == "system":
                self._system_messages.append(m)
            else:
                self._messages.append(m)

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
        for m in data.get("messages", []):
            try:
                role, content = m["role"], m["content"]
            except (KeyError, TypeError) as e:
                raise ValueError(f"invalid message entry: {m!r}") from e
            msg = Message(role, content)
            if msg.role == "system":
                buffer._system_messages.append(msg)
            else:
                buffer._messages.append(msg)
        return buffer
