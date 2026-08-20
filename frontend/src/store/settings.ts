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
