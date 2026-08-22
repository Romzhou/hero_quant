import {fireEvent, render, screen, waitFor} from "@testing-library/react"
import Chat from "../pages/Chat"
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
