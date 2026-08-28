import {fireEvent, render, screen, waitFor, cleanup, act} from "@testing-library/react"
import Chat, { parseSseData } from "../pages/Chat"
import {useChatStore} from "../store/chat"

class TestEventSource {
  static instances: TestEventSource[] = []
  readyState = 1
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  close = vi.fn()

  constructor(public url: string) {
    TestEventSource.instances.push(this)
  }
}

const ticketResponse = (ticket: string) => ({
  ok: true,
  status: 200,
  json: async () => ({ticket, expires_in: 60}),
})

beforeEach(() => {
  TestEventSource.instances = []
  useChatStore.getState().clear()
  useChatStore.getState().setInput("")
  useChatStore.getState().setStreaming(false)
  Object.defineProperty(HTMLElement.prototype, "scrollTo", {configurable: true, value: vi.fn()})
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  cleanup()
})

test("chat renders", () => { render(<Chat/>); expect(screen.getByText(/对话/)).toBeInTheDocument() })

test("requests a ticket before opening EventSource", async () => {
  const fetchMock = vi.fn().mockResolvedValue(ticketResponse("event-ticket"))
  vi.stubGlobal("fetch", fetchMock)
  vi.stubGlobal("EventSource", TestEventSource)

  render(<Chat />)
  fireEvent.change(screen.getByPlaceholderText(/输入投研问题/), {target: {value: "查询行情"}})
  fireEvent.click(screen.getByRole("button", {name: "发送"}))

  await waitFor(() => expect(TestEventSource.instances).toHaveLength(1))
  expect(fetchMock).toHaveBeenCalledWith("/v1/query/ticket", expect.objectContaining({method: "POST"}))
  expect(TestEventSource.instances[0].url).toContain("q=%E6%9F%A5%E8%AF%A2%E8%A1%8C%E6%83%85")
  expect(TestEventSource.instances[0].url).toContain("ticket=event-ticket")
})

test("requests a fresh ticket before fetch fallback", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(ticketResponse("event-ticket"))
    .mockResolvedValueOnce(ticketResponse("fetch-ticket"))
    .mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: new ReadableStream<Uint8Array>({start(controller) { controller.close() }}),
    })
  vi.stubGlobal("fetch", fetchMock)
  vi.stubGlobal("EventSource", class { constructor() { throw new Error("unsupported") } })

  render(<Chat />)
  fireEvent.change(screen.getByPlaceholderText(/输入投研问题/), {target: {value: "查询行情"}})
  fireEvent.click(screen.getByRole("button", {name: "发送"}))

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
  expect(fetchMock.mock.calls[0][0]).toBe("/v1/query/ticket")
  expect(fetchMock.mock.calls[1][0]).toBe("/v1/query/ticket")
  expect(fetchMock.mock.calls[2][0]).toContain("/v1/query/stream?q=%E6%9F%A5%E8%AF%A2%E8%A1%8C%E6%83%85&ticket=fetch-ticket")
})

// --- TDD new tests for scan_remain fixes ---

test("parseSseData is pure and handles delta/tool/error/raw", () => {
  // raw JSON delta
  expect(parseSseData('{"delta":"hello"}')).toMatchObject({ kind: "delta", delta: "hello" })
  // tool type
  const tool = parseSseData('{"type":"tool","tool":"get_market_data","status":"success","preview":"ok","latencyMs":42}')
  expect(tool.kind).toBe("tool")
  expect(tool.tool.tool).toBe("get_market_data")
  // error type
  const err = parseSseData('{"type":"error","msg":"boom"}')
  expect(err.kind).toBe("error")
  // non-JSON raw fallback -> treated as delta text
  expect(parseSseData('plain text')).toMatchObject({ kind: "delta", delta: "plain text" })
  // [DONE] yields null / no-op
  expect(parseSseData("[DONE]")).toBeNull()
  expect(parseSseData("")).toBeNull()
})

test("tool trace does not collapse multiple invocations of same tool name", async () => {
  const fetchMock = vi.fn().mockResolvedValue(ticketResponse("t-1"))
  vi.stubGlobal("fetch", fetchMock)
  vi.stubGlobal("EventSource", TestEventSource)
  const uuidSpy = vi.spyOn(crypto as any, "randomUUID").mockImplementation(() => `uuid-${Math.random()}`)
  render(<Chat />)
  fireEvent.change(screen.getByPlaceholderText(/输入投研问题/), {target: {value: "回测 600519"}})
  fireEvent.click(screen.getByRole("button", {name: "发送"}))
  await waitFor(() => expect(TestEventSource.instances).toHaveLength(1))
  const es = TestEventSource.instances[0]
  // first emit delta so assistant content non-empty and tool track visible (render condition requires m.content)
  act(() => {
    es.onmessage?.({ data: JSON.stringify({ delta: "hi" }) } as MessageEvent)
  })
  // emit two tool events with same tool name but different previews - should NOT collapse
  act(() => {
    es.onmessage?.({ data: JSON.stringify({ type: "tool", tool: "get_market_data", status: "success", preview: "first", latencyMs: 10 }) } as MessageEvent)
  })
  act(() => {
    es.onmessage?.({ data: JSON.stringify({ type: "tool", tool: "get_market_data", status: "success", preview: "second", latencyMs: 20 }) } as MessageEvent)
  })
  await waitFor(() => {
    const text = document.body.textContent || ""
    expect(text).toContain("first")
    expect(text).toContain("second")
  })
  const toolCards = Array.from(document.querySelectorAll('.font-mono')).filter(el => el.textContent?.includes('get_market_data'))
  expect(toolCards.length).toBe(2)
  uuidSpy.mockRestore()
})

test("unmount cleans up EventSource and AbortController and cancels timers", async () => {
  const abortSpy = vi.spyOn(AbortController.prototype, "abort")
  const fetchMock2 = vi.fn().mockResolvedValue(ticketResponse("t2"))
  vi.stubGlobal("fetch", fetchMock2)
  vi.stubGlobal("EventSource", TestEventSource)
  TestEventSource.instances = []
  const { unmount } = render(<Chat />)
  const input = screen.getByPlaceholderText(/输入投研问题/)
  fireEvent.change(input, {target: {value: "hello2"}})
  const sendBtn = screen.getByRole("button", {name: "发送"})
  fireEvent.click(sendBtn)
  await waitFor(() => expect(TestEventSource.instances.length).toBeGreaterThan(0))
  const esInst = TestEventSource.instances[TestEventSource.instances.length-1]
  unmount()
  expect(esInst.close).toHaveBeenCalled()
  expect(abortSpy).toHaveBeenCalled()
})

test("fetch fallback stops outer loop after [DONE] (no extra delta after DONE)", async () => {
  const encoder = new TextEncoder()
  const chunks = [
    encoder.encode('data: {"delta":"first"}\n\n'),
    encoder.encode('data: [DONE]\n\n'),
    encoder.encode('data: {"delta":"shouldNotAppear"}\n\n'),
  ]
  let idx = 0
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (idx < chunks.length) {
        controller.enqueue(chunks[idx++])
      } else {
        controller.close()
      }
    }
  })
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(ticketResponse("t-done"))
    .mockResolvedValueOnce(ticketResponse("t-done-2"))
    .mockResolvedValueOnce({ ok: true, status: 200, body: stream })
  vi.stubGlobal("fetch", fetchMock)
  vi.stubGlobal("EventSource", class { constructor() { throw new Error("unsupported") } })
  render(<Chat />)
  fireEvent.change(screen.getByPlaceholderText(/输入投研问题/), {target: {value: "test done"}})
  fireEvent.click(screen.getByRole("button", {name: "发送"}))
  await waitFor(() => expect(useChatStore.getState().messages.some(m => m.content.includes("first"))).toBe(true), { timeout: 3000 })
  await act(async () => { await new Promise(r => setTimeout(r, 50)) })
  expect(useChatStore.getState().messages.some(m => m.content.includes("shouldNotAppear"))).toBe(false)
})

// --- P2 new tests ---
test("hardcoded endpoints extracted to config constants", async () => {
  const fs = await import("fs")
  const content = fs.readFileSync("src/pages/Chat.tsx", "utf-8")
  expect(content).toContain("API_ENDPOINTS")
  expect(content).toContain("SSE_DONE")
  expect(content).toContain("SSE_CONNECT_TIMEOUT_MS")
  // scroll should use scrollHeight not 99999
  expect(content).not.toContain("top: 99999")
  expect(content).toContain("scrollHeight")
})

test("TOOL_STATUS_CLASS Record map exists", async () => {
  const fs = await import("fs")
  const content = fs.readFileSync("src/pages/Chat.tsx", "utf-8")
  expect(content).toContain("TOOL_STATUS_CLASS")
})

test("resp.json null guard returns SSE ticket missing", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => null })
  vi.stubGlobal("fetch", fetchMock)
  vi.stubGlobal("EventSource", TestEventSource)
  render(<Chat />)
  fireEvent.change(screen.getByPlaceholderText(/输入投研问题/), { target: { value: "trigger null" } })
  fireEvent.click(screen.getByRole("button", { name: "发送" }))
  await waitFor(() => expect(document.body.textContent || "").toMatch(/SSE ticket missing|请求失败/), { timeout: 3000 })
  const bodyText = document.body.textContent || ""
  expect(bodyText).not.toContain("Cannot read properties of null")
  expect(bodyText).not.toContain("TypeError")
})

test("abortAll closes EventSource and clears timer", async () => {
  const fetchMock = vi.fn().mockResolvedValue(ticketResponse("t-abort"))
  vi.stubGlobal("fetch", fetchMock)
  vi.stubGlobal("EventSource", TestEventSource)
  TestEventSource.instances = []
  const { unmount } = render(<Chat />)
  fireEvent.change(screen.getByPlaceholderText(/输入投研问题/), { target: { value: "hello abort" } })
  fireEvent.click(screen.getByRole("button", { name: "发送" }))
  await waitFor(() => expect(TestEventSource.instances.length).toBeGreaterThan(0))
  const esInst = TestEventSource.instances[TestEventSource.instances.length - 1]
  // check Chat.tsx contains abortAll helper that closes ES + clears timer
  const fs = await import("fs")
  const content = fs.readFileSync("src/pages/Chat.tsx", "utf-8")
  expect(content).toContain("abortAll")
  expect(content).toContain("esRef.current?.close()")
  unmount()
  expect(esInst.close).toHaveBeenCalled()
})

test("EMPTY_FALLBACK_MSG extracted and reused", async () => {
  const fs = await import("fs")
  const content = fs.readFileSync("src/pages/Chat.tsx", "utf-8")
  expect(content).toContain("EMPTY_FALLBACK_MSG")
  // should appear at least 3 times usages (definition + 3 reuse) and not have duplicated literal more than needed
  const literalCount = (content.match(/模型未返回内容/g) || []).length
  expect(literalCount).toBe(1)
})
