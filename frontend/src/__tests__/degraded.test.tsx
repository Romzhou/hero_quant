/**
 * E线：降级横幅语义锁定（vitest）
 * - helpers：显式 degraded 载荷 / reason 提取 / 文本兜底 / 503 判定
 * - parseSseData：degraded 载荷路由为横幅，不进入正文/轨迹
 * - DegradedBanner：role=alert + data-testid + 兜底文案
 * - Chat 集成：degraded SSE 事件 / 合成演示文本均点亮横幅
 * - App Shell：/ready 503 时全局横幅（消费 D 线语义，不改 server.py）
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import App from "../App"
import DegradedBanner from "../components/DegradedBanner"
import {
  DEGRADED_FALLBACK_REASON,
  NO_DIST_503_HINT,
  detectDegradedFromText,
  extractDegradedReason,
  isDegradedPayload,
  isServiceUnavailable,
} from "../config/degraded"
import Chat, { parseSseData } from "../pages/Chat"
import { useChatStore } from "../store/chat"

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  cleanup()
})

// ---------- helpers ----------

test("isDegradedPayload 识别显式降级载荷", () => {
  expect(isDegradedPayload({ degraded: true, reason: "fake llm" })).toBe(true)
  expect(isDegradedPayload({ type: "degraded", reason: "emulated" })).toBe(true)
  expect(isDegradedPayload({ delta: "hi" })).toBe(false)
  expect(isDegradedPayload(null)).toBe(false)
  expect(isDegradedPayload("degraded")).toBe(false)
})

test("extractDegradedReason 按 reason/msg/message 优先级提取", () => {
  expect(extractDegradedReason({ degraded: true, reason: "fake llm" })).toBe("fake llm")
  expect(extractDegradedReason({ degraded: true, msg: "m" })).toBe("m")
  expect(extractDegradedReason({ degraded: true })).toBeNull()
  expect(extractDegradedReason(null)).toBeNull()
})

test("detectDegradedFromText 命中演示标记时返回兜底原因", () => {
  expect(detectDegradedFromText("正常文本")).toBeNull()
  const hit = detectDegradedFromText("（演示模式：未配置 HERO_API_KEY，数据为合成占位）")
  expect(hit).toBe(DEGRADED_FALLBACK_REASON)
})

test("isServiceUnavailable 仅 503 为真", () => {
  expect(isServiceUnavailable(503)).toBe(true)
  expect(isServiceUnavailable(200)).toBe(false)
  expect(isServiceUnavailable(500)).toBe(false)
  expect(NO_DIST_503_HINT).toContain("npm run build")
})

// ---------- parseSseData ----------

test("parseSseData 将 degraded 载荷路由为横幅", () => {
  const p = parseSseData('{"degraded":true,"reason":"fake llm"}')
  expect(p?.kind).toBe("degraded")
  expect(p?.degraded?.reason).toBe("fake llm")
  const noReason = parseSseData('{"type":"degraded"}')
  expect(noReason?.kind).toBe("degraded")
  expect(noReason?.degraded?.reason).toBeNull()
  // 既有路由不受影响
  expect(parseSseData('{"delta":"hi"}')).toMatchObject({ kind: "delta", delta: "hi" })
})

// ---------- DegradedBanner ----------

test("DegradedBanner 渲染 reason 与来源", () => {
  render(<DegradedBanner reason="fake llm" source="Chat SSE" />)
  const banner = screen.getByTestId("degraded-banner")
  expect(banner).toHaveAttribute("role", "alert")
  expect(banner.textContent).toContain("fake llm")
  expect(banner.textContent).toContain("Chat SSE")
})

test("DegradedBanner 无 reason 时用兜底文案（含D线依赖声明）", () => {
  render(<DegradedBanner reason={null} />)
  expect(screen.getByTestId("degraded-banner").textContent).toContain("依赖D线")
})

// ---------- Chat 集成 ----------

class StubES {
  static instances: StubES[] = []
  readyState = 1
  onmessage: ((e: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  close = vi.fn()
  url = ""
  constructor(url: string) {
    this.url = url
    StubES.instances.push(this)
  }
}

const ticketOk = (ticket: string) => ({
  ok: true,
  status: 200,
  json: async () => ({ ticket, expires_in: 60 }),
})

async function startChatStream() {
  StubES.instances = []
  useChatStore.getState().clear()
  useChatStore.getState().setInput("")
  useChatStore.getState().setStreaming(false)
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(ticketOk("deg-ticket")))
  vi.stubGlobal("EventSource", StubES)
  render(<Chat />)
  fireEvent.change(screen.getByPlaceholderText(/输入投研问题/), { target: { value: "回测 600519" } })
  fireEvent.click(screen.getByRole("button", { name: "发送" }))
  await waitFor(() => expect(StubES.instances).toHaveLength(1))
  return StubES.instances[0]
}

test("Chat 收到 degraded SSE 事件后点亮横幅（直显后端 reason）", async () => {
  const es = await startChatStream()
  act(() => {
    es.onmessage?.({ data: JSON.stringify({ degraded: true, reason: "fake llm e2e" }) } as MessageEvent)
  })
  await waitFor(() => expect(screen.getByTestId("degraded-banner")).toBeInTheDocument())
  expect(screen.getByTestId("degraded-banner").textContent).toContain("fake llm e2e")
})

test("Chat 收到合成演示文本时经兜底点亮横幅（D线未落地兼容）", async () => {
  const es = await startChatStream()
  act(() => {
    es.onmessage?.({ data: JSON.stringify({ delta: "结论：等权跑赢基准（演示模式：合成占位）" }) } as MessageEvent)
  })
  await waitFor(() => expect(screen.getByTestId("degraded-banner")).toBeInTheDocument())
  expect(screen.getByTestId("degraded-banner").textContent).toContain("依赖D线")
})

// ---------- App Shell /ready 503 ----------

test("App Shell 在 /ready 503 时展示全局横幅", async () => {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (typeof url === "string" && url.includes("/ready")) {
      return Promise.resolve({
        ok: false,
        status: 503,
        json: async () => ({ status: "degraded", pg: false, checkpoint: "pg-emulated", billing: "off" }),
      })
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => ({ annual_return: 0.12, sharpe: 1.1, max_drawdown: -0.02, turnover: 0.3 }),
    })
  })
  vi.stubGlobal("fetch", fetchMock)
  render(
    <MemoryRouter initialEntries={["/dashboard"]}>
      <App />
    </MemoryRouter>
  )
  const banner = await screen.findByTestId("degraded-banner", {}, { timeout: 4000 })
  expect(banner.textContent).toContain("/ready 503")
})

test("App Shell 在 /ready ok 时不打扰（无横幅）", async () => {
  const fetchMock = vi.fn().mockImplementation(() =>
    Promise.resolve({
      ok: true,
      status: 200,
      json: async () => ({ status: "ok", annual_return: 0.12, sharpe: 1.1, max_drawdown: -0.02, turnover: 0.3 }),
    })
  )
  vi.stubGlobal("fetch", fetchMock)
  render(
    <MemoryRouter initialEntries={["/dashboard"]}>
      <App />
    </MemoryRouter>
  )
  await screen.findByText(/Dashboard/, {}, { timeout: 4000 })
  expect(screen.queryByTestId("degraded-banner")).toBeNull()
})
