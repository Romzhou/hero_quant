/**
 * 聊天状态（Zustand）
 * - 管什么：messages（对话历史，含 welcome 初始问候）、input（输入框）、streaming（是否流式中）
 * - 操作：setInput/push/setStreaming/clear
 * - 被谁消费：pages/Chat.tsx 订阅 messages/streaming 驱动消息列表与 SSE 逐 delta 追加，快捷问句与输入框写入 input
 */
import { create } from "zustand"

type Msg = { role: "user" | "assistant"; content: string; id: string }

interface ChatState {
  messages: Msg[]
  input: string
  streaming: boolean
  setInput: (v: string) => void
  push: (m: Msg) => void
  setStreaming: (v: boolean) => void
  clear: () => void
}

export const useChatStore = create<ChatState>((set) => ({
  messages: [
    { id: "welcome", role: "assistant", content: "你好，我是真英雄量化助手。输入「回测 600519.SH 近一月」或任意投研问题开始。" }
  ],
  input: "",
  streaming: false,
  setInput: (input) => set({ input }),
  push: (m) => set((s) => ({ messages: [...s.messages, m] })),
  setStreaming: (streaming) => set({ streaming }),
  clear: () => set({ messages: [] })
}))
