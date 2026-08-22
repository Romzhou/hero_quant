import {render, waitFor} from "@testing-library/react"
import Monitor from "../pages/Monitor"

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
