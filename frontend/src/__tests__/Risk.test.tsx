import { render, screen, waitFor, fireEvent } from "@testing-library/react"
import Risk from "../pages/Risk"

const SUMMARY = "/v1/risk/summary"
const ATTRIBUTION = "/v1/risk/attribution"
const SHADOW = "/v1/risk/shadow"

/** 按 URL 分发：三个端点各自返回自己的真值体，失败 URL 走 404。 */
function stubFetch(map: Record<string, unknown>) {
  const fn = vi.fn(async (url: string) => {
    if (url in map) return { ok: true, status: 200, json: async () => map[url] }
    return { ok: false, status: 404, json: async () => ({}) }
  })
  vi.stubGlobal("fetch", fn as unknown as typeof fetch)
  return fn
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

test("fetch rejects → error banner visible, badge shows degraded state, 风控正常 absent", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")))
  render(<Risk />)
  await waitFor(() => expect(screen.getByText(/风控数据获取失败/)).toBeInTheDocument())
  expect(screen.getByRole("button", { name: /重试/ })).toBeInTheDocument()
  // badge degraded
  expect(screen.getByText(/数据异常/)).toBeInTheDocument()
  expect(screen.queryByText(/风控正常/)).not.toBeInTheDocument()
})

test("degraded=true → 降级横幅展示 reason，不再出现占位数据措辞", async () => {
  stubFetch({
    [SUMMARY]: { exposure: 0.33, turnover: 0.5, circuit: "CLOSED", degraded: true, reason: "circuit_threshold_missing" },
    [ATTRIBUTION]: {
      components: { timing: null, selection: null, cost: null, risk_control: null, residual: null },
      coverage: 0,
      bars: 0,
      degraded: true,
      reason: "positions_missing",
    },
    [SHADOW]: { orders_total: null, reject_rate: null, degraded: true, reason: "no_fills" },
  })
  render(<Risk />)
  await waitFor(() => expect(screen.getByText(/风控数据降级/)).toBeInTheDocument())
  expect(screen.getByText(/总览：circuit_threshold_missing/)).toBeInTheDocument()
  expect(screen.getByText(/归因：positions_missing/)).toBeInTheDocument()
  expect(screen.getByText(/影子重放：no_fills/)).toBeInTheDocument()
  expect(screen.queryByText(/占位数据/)).not.toBeInTheDocument()
  expect(screen.getByText(/数据异常/)).toBeInTheDocument()
})

test("真实归因/影子重放渲染真值，null 渲染 --", async () => {
  stubFetch({
    [SUMMARY]: { exposure: 0.33, single_limit: 0.2, circuit_threshold: 0.5, turnover: 0.42, reject_rate: 0.1, circuit: "CLOSED", cross_source: "pass", pit: "verified" },
    [ATTRIBUTION]: {
      components: { timing: 0.012, selection: null, cost: -0.0005, risk_control: -0.05, residual: 0.07 },
      not_applicable: ["selection_single_symbol"],
      coverage: 0.8,
      net_return: 0.0815,
      metrics_cumulative_return: 0.21,
      turnover_notional: 210,
      bars: 5,
      degraded: false,
    },
    [SHADOW]: { orders_total: 6, allowed: 6, rejected: 0, rejected_by_rule: {}, reject_rate: 0, notional_total: 210, bars: 6, degraded: false },
  })
  const { container } = render(<Risk />)
  await waitFor(() => expect(screen.getByText(/风控正常 · CLOSED/)).toBeInTheDocument())
  expect(screen.getByText("33.0%")).toBeInTheDocument()
  expect(screen.getByText("20.0%")).toBeInTheDocument()
  // 归因真值：择时 1.20% / 成本 -0.05% / 其他 7.00%，选股不可算 → --
  expect(screen.getByText("1.20%")).toBeInTheDocument()
  expect(screen.getByText("-0.05%")).toBeInTheDocument()
  expect(screen.getByText("7.00%")).toBeInTheDocument()
  expect(screen.getByText(/coverage 80%/)).toBeInTheDocument() // coverage 0.8
  expect(screen.getByText(/selection_single_symbol/)).toBeInTheDocument()
  // 影子重放真值
  expect(screen.getByText("0.00%")).toBeInTheDocument() // reject_rate 0
  expect(container.textContent).not.toContain("NaN%")
  expect(screen.queryByText(/风控数据降级/)).not.toBeInTheDocument()
})

test("malformed payload → 不崩，无效字段渲染 --", async () => {
  stubFetch({
    [SUMMARY]: { exposure: "62%", single_limit: "20%", circuit_threshold: "bad", turnover: "oops", circuit: 123 },
    [ATTRIBUTION]: { components: { timing: "x", selection: null }, coverage: "y", bars: "z" },
    [SHADOW]: { orders_total: "x", reject_rate: null, rejected_by_rule: "bad" },
  })
  const { container } = render(<Risk />)
  await waitFor(() => expect(screen.getByText("总敞口")).toBeInTheDocument())
  expect(container.textContent).not.toContain("NaN%")
  expect(screen.getAllByText("--").length).toBeGreaterThan(0)
  expect(container.textContent).not.toContain("62%")
})

test("retry button re-runs probe after failure", async () => {
  let fail = true
  const fetchMock = vi.fn(async (url: string) => {
    if (fail) throw new Error("first fail")
    const map: Record<string, unknown> = {
      [SUMMARY]: { exposure: 0.33, circuit: "CLOSED" },
      [ATTRIBUTION]: { components: { timing: 0.01 }, coverage: 0.8, bars: 5, degraded: false },
      [SHADOW]: { orders_total: 6, reject_rate: 0, degraded: false },
    }
    return { ok: true, status: 200, json: async () => map[url] }
  })
  vi.stubGlobal("fetch", fetchMock)
  render(<Risk />)
  await waitFor(() => expect(screen.getByText(/风控数据获取失败/)).toBeInTheDocument())
  fail = false
  fireEvent.click(screen.getByRole("button", { name: /重试/ }))
  await waitFor(() => expect(screen.getByText(/风控正常 · CLOSED/)).toBeInTheDocument())
  expect(fetchMock).toHaveBeenCalledTimes(6) // 3 端点 × 2 轮
  expect(screen.queryByText(/风控数据获取失败/)).not.toBeInTheDocument()
})
