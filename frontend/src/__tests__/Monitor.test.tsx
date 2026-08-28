import {render, waitFor, cleanup, act} from "@testing-library/react"
import Monitor from "../pages/Monitor"
import Live from "../pages/Live"

const unavailable = {ok: false, status: 404, body: null}
const ticketResponse = {
  ok: true,
  status: 200,
  json: async () => ({ticket: "monitor-ticket", expires_in: 60}),
}
const emptyStream = {
  ok: true,
  status: 200,
  body: new ReadableStream<Uint8Array>({start(controller) { controller.close() }}),
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  cleanup()
})

test("query stream candidate gets a ticket before connecting", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(unavailable)
    .mockResolvedValueOnce(unavailable)
    .mockResolvedValueOnce(ticketResponse)
    .mockResolvedValueOnce(emptyStream)
  vi.stubGlobal("fetch", fetchMock)
  Object.defineProperty(HTMLElement.prototype, "scrollTo", {configurable: true, value: vi.fn()})
  vi.spyOn(Math, "random").mockReturnValue(1)

  render(<Monitor />)

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4))
  expect(fetchMock.mock.calls[2][0]).toBe("/v1/query/ticket")
  expect(fetchMock.mock.calls[2][1]).toEqual(expect.objectContaining({method: "POST"}))
  expect(fetchMock.mock.calls[3][0]).toBe("/v1/query/stream?offset=4&ticket=monitor-ticket")
})

test("Monitor effect deps stable - offset/cost change does not trigger reconnect storm", async () => {
  // Mock a long-lived stream that pushes one event then stays open
  const encoder = new TextEncoder()
  let controllerRef: ReadableStreamDefaultController<Uint8Array> | null = null
  const stream = new ReadableStream<Uint8Array>({
    start(controller) { controllerRef = controller },
  })
  const fetchMock = vi.fn().mockImplementation(async (url: string) => {
    if (typeof url === "string" && url.includes("/v1/query/ticket")) return ticketResponse
    return { ok: true, status: 200, body: stream }
  })
  vi.stubGlobal("fetch", fetchMock)
  Object.defineProperty(HTMLElement.prototype, "scrollTo", {configurable: true, value: vi.fn()})
  class FakeES { onmessage: any=null; onerror: any=null; close=vi.fn(); constructor(public url:string){} }
  vi.stubGlobal("EventSource", FakeES as any)

  render(<Monitor />)
  // initial fetch should be 1 (first candidate)
  await waitFor(() => expect(fetchMock).toHaveBeenCalled(), { timeout: 2000 })
  const callsAfterMount = fetchMock.mock.calls.length
  // push an event that will update offset/cost
  act(() => {
    controllerRef?.enqueue(encoder.encode('data: {"offset":10,"type":"tool","msg":"update","cost":4.0}\n\n'))
  })
  // allow state to update
  await act(async () => { await new Promise(r => setTimeout(r, 50)) })
  // wait a bit more for potential reconnection
  await act(async () => { await new Promise(r => setTimeout(r, 100)) })
  // should NOT have triggered a new fetch due to offset change
  expect(fetchMock.mock.calls.length).toBe(callsAfterMount)
})

test("Monitor cleanup cancels reader and aborts controller", async () => {
  const cancelSpy = vi.fn().mockResolvedValue(undefined)
  const mockReader = {
    read: vi.fn().mockImplementation(() => new Promise(() => {})), // never resolves
    cancel: cancelSpy,
    releaseLock: vi.fn(),
  } as unknown as ReadableStreamDefaultReader<Uint8Array>
  const mockStream = {
    getReader: () => mockReader,
  } as unknown as ReadableStream<Uint8Array>
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, body: mockStream })
  vi.stubGlobal("fetch", fetchMock)
  Object.defineProperty(HTMLElement.prototype, "scrollTo", {configurable: true, value: vi.fn()})
  class FakeES { onmessage: any=null; onerror: any=null; close=vi.fn(); constructor(public url:string){} }
  vi.stubGlobal("EventSource", FakeES as any)

  const { unmount } = render(<Monitor />)
  await waitFor(() => expect(fetchMock).toHaveBeenCalled())
  // give effect time to set readerRef
  await act(async () => { await new Promise(r => setTimeout(r, 20)) })
  unmount()
  // cleanup should have called cancel
  await waitFor(() => expect(cancelSpy).toHaveBeenCalled())
})

test("Live stale paused closure - toggling pause breaks loop via pausedRef", async () => {
  // Live should use pausedRef, not stale closure
  let readerContinue = true
  const cancelSpy = vi.fn()
  const readMock = vi.fn().mockImplementation(async () => {
    if (!readerContinue) return { done: true, value: undefined }
    // return a chunk with an event
    await new Promise(r => setTimeout(r, 10))
    return { done: false, value: new TextEncoder().encode('data: {"type":"tool","msg":"x","offset":5}\n\n') }
  })
  const mockReader = { read: readMock, cancel: cancelSpy, releaseLock: vi.fn() } as any
  const mockStream = { getReader: () => mockReader } as any
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, body: mockStream })
  vi.stubGlobal("fetch", fetchMock)
  Object.defineProperty(HTMLElement.prototype, "scrollTo", {configurable: true, value: vi.fn()})
  class FakeES { onmessage: any=null; onerror: any=null; close=vi.fn(); constructor(public url:string){} }
  vi.stubGlobal("EventSource", FakeES as any)

  const { rerender } = render(<Live />)
  await waitFor(() => expect(fetchMock).toHaveBeenCalled())
  // find pause button and click to pause
  const pauseBtn = document.querySelector('button')
  // Live has pause button text "⏸ 暂停" initially
  const btn = Array.from(document.querySelectorAll('button')).find(b => b.textContent?.includes('暂停') || b.textContent?.includes('恢复'))
  expect(btn).toBeTruthy()
  // click pause - loop should respect pausedRef
  if (btn) act(() => { (btn as HTMLButtonElement).click() })
  // after pause, read should not be called again aggressively - we check that abort or cancel was invoked
  await act(async () => { await new Promise(r => setTimeout(r, 50)) })
  // the test passes if no error and loop respects pause (we verify readMock call count stabilizes)
  const callsAfterPause = readMock.mock.calls.length
  await act(async () => { await new Promise(r => setTimeout(r, 40)) })
  // if stale closure bug exists, loop would keep calling read even after pause; with fix it should stop increasing rapidly
  // we allow at most 1 extra call after pause
  expect(readMock.mock.calls.length - callsAfterPause).toBeLessThanOrEqual(1)
  readerContinue = false
})

test("Live AbortController leak - previous controller aborted before overwrite", async () => {
  const abortSpies: vi.Mock[] = []
  const origAbort = global.AbortController
  class TrackingAbortController extends AbortController {
    abort = vi.fn(() => super.abort())
    constructor() { super(); abortSpies.push(this.abort as any) }
  }
  vi.stubGlobal("AbortController", TrackingAbortController as any)
  // Make first fetch candidate fail, second succeed to trigger candidate retry loop
  const encoder = new TextEncoder()
  const stream = new ReadableStream<Uint8Array>({
    start(c) { c.enqueue(encoder.encode('data: {"type":"tool","msg":"ok"}\n\n')); c.close() }
  })
  const fetchMock = vi.fn()
    .mockImplementationOnce(async () => ({ ok: false, status: 500, body: null }))
    .mockImplementationOnce(async () => ({ ok: true, status: 200, body: stream }))
  vi.stubGlobal("fetch", fetchMock)
  Object.defineProperty(HTMLElement.prototype, "scrollTo", {configurable: true, value: vi.fn()})
  class FakeES { onmessage:any=null; onerror:any=null; close=vi.fn(); constructor(public url:string){} }
  vi.stubGlobal("EventSource", FakeES as any)

  render(<Live />)
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2), { timeout: 2000 })
  // first controller should have been aborted before second candidate
  // At least one abort should have been called
  expect(abortSpies.length).toBeGreaterThanOrEqual(2)
  expect(abortSpies[0]).toHaveBeenCalled()
  vi.stubGlobal("AbortController", origAbort as any)
})
