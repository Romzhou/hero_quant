/**
 * 设置状态（Zustand）
 * - 管什么：apiBase（空则同源 /v1，填绝对地址可覆盖代理）、model（默认 gpt-4o-mini）
 * - 被谁消费：pages/Settings.tsx 读写本地偏好；后续请求层可读取 apiBase 决定 fetch 基路径
 */
import { create } from "zustand"

interface SettingsState {
  apiBase: string
  model: string
  setApiBase: (v: string) => void
  setModel: (v: string) => void
}

export const useSettingsStore = create<SettingsState>((set) => ({
  apiBase: "",
  model: "gpt-4o-mini",
  setApiBase: (apiBase) => set({ apiBase }),
  setModel: (model) => set({ model })
}))
