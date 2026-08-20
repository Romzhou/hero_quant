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
