import {render, screen, waitFor, fireEvent, cleanup} from "@testing-library/react"
import {BrowserRouter} from "react-router-dom"
import Dashboard from "../pages/Dashboard"

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  cleanup()
})

const wrap = (ui: React.ReactNode) => <BrowserRouter>{ui}</BrowserRouter>

test("Dashboard shows error banner with honest copy and retry on fetch failure", async () => {
  const errSpy = vi.spyOn(console, "error").mockImplementation(() => {})
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")))
  render(wrap(<Dashboard />))
  await waitFor(() => expect(screen.getByText(/数据获取失败，显示为占位数据/)).toBeInTheDocument(), { timeout: 2000 })
  expect(screen.getByRole("button", { name: /重试/ })).toBeInTheDocument()
  expect(errSpy).toHaveBeenCalled()
})

test("Dashboard retry button re-runs load after failure", async () => {
  const errSpy = vi.spyOn(console, "error").mockImplementation(() => {})
  const fetchMock = vi.fn()
    .mockRejectedValueOnce(new Error("first fail"))
    .mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ annual_return: 0.12, sharpe: 1.1, max_drawdown: -0.02, turnover: 0.3, total_equity: 12345 }),
    })
  vi.stubGlobal("fetch", fetchMock)
  const { container } = render(wrap(<Dashboard />))
  await waitFor(() => expect(screen.getByText(/数据获取失败，显示为占位数据/)).toBeInTheDocument())
  fireEvent.click(screen.getByRole("button", { name: /重试/ }))
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  await waitFor(() => expect(screen.queryByText(/数据获取失败，显示为占位数据/)).not.toBeInTheDocument())
  // should now show real metrics, not fallback — use container check to avoid multiple-match throw
  await waitFor(() => expect(container.textContent).toContain("12.0%"))
  errSpy.mockRestore()
})

test("Dashboard fmtPct fallback prevents NaN% on invalid metrics", async () => {
  // NaN and Infinity should not render as NaN%
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ annual_return: NaN, sharpe: Infinity, max_drawdown: NaN, turnover: NaN }),
  }))
  const { container } = render(wrap(<Dashboard />))
  await waitFor(() => expect(container.textContent).not.toContain("…"), { timeout: 2000 })
  expect(container.textContent).not.toContain("NaN%")
  expect(container.textContent).not.toContain("Infinity%")
  // should show fallback formatted values or placeholder "--"/fallback
  // Check that年化卡 not NaN
  const cards = container.textContent || ""
  expect(cards).not.toMatch(/NaN/)
})

test("Dashboard handles missing annual_return gracefully (fallback)", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ sharpe: 1.5 }), // missing annual_return, max_drawdown
  }))
  const { container } = render(wrap(<Dashboard />))
  await waitFor(() => expect(container.textContent).not.toContain("…"), { timeout: 2000 })
  expect(container.textContent).not.toContain("NaN%")
  // should render fallback 18.4% (0.184) for missing annual_return
  expect(container.textContent).toContain("18.4%")
})
