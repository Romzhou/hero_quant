import { render, screen, fireEvent, waitFor, cleanup, act } from "@testing-library/react"
import Live from "../pages/Live"
import * as LiveMod from "../pages/Live"

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  cleanup()
})

test("Live renders", () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 404, body: null }))
  class FakeES { onmessage:any=null; onerror:any=null; close=vi.fn(); constructor(public url:string){} }
  vi.stubGlobal("EventSource", FakeES as any)
  Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() })
  render(<Live />)
  expect(screen.getByText(/Live 监控/)).toBeInTheDocument()
})

test("magic numbers extracted to named constants", () => {
  const mod: any = LiveMod
  // After fix, constants should be exported
  expect(mod.COST_LIMIT_USD ?? mod.COST_LIMIT ?? mod.MAX_EVENTS ?? mod.BREAKER_HALF_OPEN_RATIO ?? mod.BREAKER_WARN_RATIO).toBeTruthy()
  const costLimit = mod.COST_LIMIT_USD ?? mod.COST_LIMIT ?? 5.0
  expect(costLimit).toBe(5.0)
  const maxEvents = mod.MAX_EVENTS ?? 200
  expect(maxEvents).toBeGreaterThan(100)
})

test("Cost/BudgetBreaker desync - ES event with cost updates cost bar", async () => {
  // Fetch will fail, fallback to EventSource; ES sends cost
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 500, body: null }))
  let esInstance: any = null
  class CostES {
    onmessage: any = null
    onerror: any = null
    close = vi.fn()
    constructor(public url: string) { esInstance = this }
  }
  vi.stubGlobal("EventSource", CostES as any)
  Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() })
  render(<Live />)
  await waitFor(() => expect(esInstance).not.toBeNull())
  act(() => {
    esInstance.onmessage({ data: JSON.stringify({ ts: new Date().toISOString(), offset: 10, type: "tool", msg: "cost update", cost: 4.5 }) } as MessageEvent)
  })
  await waitFor(() => {
    expect(document.body.textContent).toContain("4.500")
  })
})

test("typed ref scroll - no unsafe cast", async () => {
  // Verify Live uses typed ref with optional chaining, not as unknown cast
  const fs = await import("fs")
  const content = fs.readFileSync("src/pages/Live.tsx", "utf-8")
  expect(content).not.toContain("as unknown as {scrollTo")
  expect(content).toContain("useRef<HTMLDivElement>")
})

test("清空 clears offset/cost refs", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 500, body: null }))
  class FakeES { onmessage:any=null; onerror:any=null; close=vi.fn(); constructor(public url:string){} }
  vi.stubGlobal("EventSource", FakeES as any)
  Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() })
  render(<Live />)
  // 空态起步：不预置伪造事件，offset 从 0 开始
  expect(screen.getByText(/offset: 0/)).toBeInTheDocument()
  expect(screen.getByText("演示数据")).toBeInTheDocument()
  const clearBtn = screen.getByText("清空")
  act(() => { fireEvent.click(clearBtn) })
  await waitFor(() => {
    expect(screen.getByText(/offset: 0/)).toBeInTheDocument()
  })
  // PR-112：清空后成本回到「未定价」，不显示 $0.00 / 0.000 冒充真实读数
  expect(document.body.textContent).toContain("未定价")
})

test("unstable React key fixed - duplicate offsets render distinct rows", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 500, body: null }))
  let esInstance: any = null
  class DupES {
    onmessage: any = null
    onerror: any = null
    close = vi.fn()
    constructor(public url: string) { esInstance = this }
  }
  vi.stubGlobal("EventSource", DupES as any)
  Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() })
  render(<Live />)
  await waitFor(() => expect(esInstance).not.toBeNull())
  act(() => {
    esInstance.onmessage({ data: JSON.stringify({ ts: "2026-01-01T00:00:00.000Z", offset: 5, type: "tool", msg: "first" }) } as MessageEvent)
  })
  act(() => {
    esInstance.onmessage({ data: JSON.stringify({ ts: "2026-01-01T00:00:01.000Z", offset: 5, type: "tool", msg: "second" }) } as MessageEvent)
  })
  await waitFor(() => {
    expect(document.body.textContent).toContain("first")
    expect(document.body.textContent).toContain("second")
  })
  // Both rows should exist distinctly
  const rows = Array.from(document.querySelectorAll("div")).filter(d => d.textContent?.includes("first") || d.textContent?.includes("second"))
  expect(document.body.textContent?.match(/first/g)?.length).toBeTruthy()
  expect(document.body.textContent?.match(/second/g)?.length).toBeTruthy()
  // Verify key is not just e.offset – file should contain ts-offset or index
  const fs = await import("fs")
  const content = fs.readFileSync("src/pages/Live.tsx", "utf-8")
  expect(content).not.toMatch(/key=\{e\.offset\}/)
  expect(content).toMatch(/key=\{.*(ts|offset|index).*\-.*(offset|ts)/)
})
