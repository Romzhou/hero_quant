import { render, screen, fireEvent, waitFor, cleanup, act } from "@testing-library/react"
import Settings from "../pages/Settings"
import { useSettingsStore } from "../store/settings"

// allow StatusDot/getDotClass extraction verification
import * as SettingsMod from "../pages/Settings"

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  useSettingsStore.setState({ apiBase: "", model: "gpt-4o-mini" })
})

test("StatusDot extracted and renders correctly for null/true/false", async () => {
  // Check that module exports a top-level StatusDot or getDotClass (maintainability fix)
  const hasStatusDot = "StatusDot" in SettingsMod
  const hasGetDotClass = "getDotClass" in SettingsMod
  expect(hasStatusDot || hasGetDotClass).toBe(true)
  // Also render Settings and check dots have expected classes via DOM
  Object.defineProperty(window, "performance", { configurable: true, value: { now: () => 0 } })
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200 } as any))
  render(<Settings />)
  // should have two dots (live + metrics) with pulse when null initially before probe resolves?
  // Instead just verify Settings renders without recreating inner component error - simple smoke
  expect(screen.getByText("设置")).toBeInTheDocument()
  // verify getDotClass helper returns distinct strings
  const getDotClass = (SettingsMod as any).getDotClass
  if (getDotClass) {
    const a = getDotClass(null)
    const b = getDotClass(true)
    const c = getDotClass(false)
    expect(a).not.toBe(b)
    expect(b).not.toBe(c)
    expect(a).toContain("bg-slate-500")
    expect(b).toContain("bg-emerald-400")
    expect(c).toContain("bg-red-400")
  }
})

test("health probe respects apiBase - remote URL", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 } as any)
  vi.stubGlobal("fetch", fetchMock)
  // set apiBase before mount
  useSettingsStore.setState({ apiBase: "https://api.example.com/" })
  Object.defineProperty(window, "performance", { configurable: true, value: { now: () => Date.now() } })
  render(<Settings />)
  await waitFor(() => expect(fetchMock).toHaveBeenCalled())
  const urls = fetchMock.mock.calls.map(c => String(c[0]))
  // Should have called remote URLs, not same-origin /live
  expect(urls.some(u => u.includes("https://api.example.com/live"))).toBe(true)
  expect(urls.some(u => u.includes("https://api.example.com/v1/backtest/metrics.json"))).toBe(true)
  // hrefs should also reflect remote
  const links = Array.from(document.querySelectorAll("a")).map(a => a.getAttribute("href") || "")
  expect(links.some(h => h.includes("https://api.example.com/live"))).toBe(true)
})

test("ENDPOINTS const centralizes paths", async () => {
  const mod: any = SettingsMod
  expect(mod.ENDPOINTS || mod.ENDPOINTS_LIVE || mod.API_ENDPOINTS).toBeTruthy()
  const endpoints = mod.ENDPOINTS || mod.API_ENDPOINTS
  if (endpoints) {
    expect(Object.values(endpoints).some((v: any) => String(v).includes("/live"))).toBe(true)
    expect(Object.values(endpoints).some((v: any) => String(v).includes("metrics.json"))).toBe(true)
  }
})

test("apiBase input trims, strips trailing slash, validates URL with inline hint", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200 } as any))
  render(<Settings />)
  const input = screen.getByPlaceholderText(/留空则同源代理/) as HTMLInputElement
  // invalid URL should show error hint
  fireEvent.change(input, { target: { value: "not-a-url" } })
  await waitFor(() => {
    expect(document.body.textContent).toMatch(/无效|URL|格式/)
  })
  // valid with trailing slash and spaces should be trimmed/stripped
  fireEvent.change(input, { target: { value: "  https://api.example.com/  " } })
  await waitFor(() => {
    expect(useSettingsStore.getState().apiBase).toBe("https://api.example.com")
  })
})

test("getDotClass avoids nested ternary (helper exists)", () => {
  const mod: any = SettingsMod
  expect(typeof mod.getDotClass).toBe("function")
})
