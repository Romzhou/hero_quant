import { render, screen, waitFor, fireEvent } from "@testing-library/react"
import Risk from "../pages/Risk"

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

test("fetch rejects → error banner visible, badge shows degraded state, 风控正常 absent", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")))
  render(<Risk />)
  await waitFor(() => expect(screen.getByText(/风控数据获取失败/)).toBeInTheDocument())
  expect(screen.getByText(/当前显示为占位数据/)).toBeInTheDocument()
  expect(screen.getByRole("button", { name: /重试/ })).toBeInTheDocument()
  // badge degraded
  expect(screen.getByText(/数据异常/)).toBeInTheDocument()
  expect(screen.queryByText(/风控正常/)).not.toBeInTheDocument()
})

test("fetch returns malformed payload (exposure:\"62%\") → no crash, \"--\" rendered", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ exposure: "62%", single_limit: "20%", circuit_threshold: "bad", turnover: "oops", circuit: 123 }),
  }))
  const { container } = render(<Risk />)
  await waitFor(() => expect(screen.getByText("总敞口")).toBeInTheDocument())
  // should not throw and should render fallback "--" for invalid numeric fields instead of NaN%
  expect(container.textContent).not.toContain("NaN%")
  expect(screen.getAllByText("--").length).toBeGreaterThan(0)
})

test("fetch ok → normal render unchanged", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({
      exposure: 0.62,
      single_limit: 0.2,
      circuit_threshold: 0.8,
      turnover: 0.42,
      cross_source: "ok",
      pit: "ok",
      circuit: "CLOSED",
    }),
  }))
  render(<Risk />)
  await waitFor(() => expect(screen.getByText(/风控正常/)).toBeInTheDocument())
  expect(screen.getByText("62%")).toBeInTheDocument()
  expect(screen.getByText("20%")).toBeInTheDocument()
  expect(screen.queryByText(/风控数据获取失败/)).not.toBeInTheDocument()
  expect(screen.getByText(/风控正常 · CLOSED/)).toBeInTheDocument()
})

test("retry button re-runs probe after failure", async () => {
  const fetchMock = vi.fn()
    .mockRejectedValueOnce(new Error("first fail"))
    .mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        exposure: 0.62,
        single_limit: 0.2,
        circuit_threshold: 0.8,
        turnover: 0.42,
        circuit: "CLOSED",
      }),
    })
  vi.stubGlobal("fetch", fetchMock)
  render(<Risk />)
  await waitFor(() => expect(screen.getByText(/风控数据获取失败/)).toBeInTheDocument())
  fireEvent.click(screen.getByRole("button", { name: /重试/ }))
  await waitFor(() => expect(screen.getByText(/风控正常 · CLOSED/)).toBeInTheDocument())
  expect(fetchMock).toHaveBeenCalledTimes(2)
  expect(screen.queryByText(/风控数据获取失败/)).not.toBeInTheDocument()
})
