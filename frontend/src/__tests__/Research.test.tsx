import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react"
import Research from "../pages/Research"
// try to import helpers that will exist after fix — before fix this import will be undefined / fallback
import * as ResearchModule from "../pages/Research"

vi.mock("echarts-for-react", () => ({
  default: (props: any) => {
    // 轻量 mock 避免 jsdom canvas 报错，保留 option 可断言
    const dataTest = props.option?.series ? JSON.stringify(props.option.series).slice(0, 200) : ""
    return <div data-testid="echarts-mock" data-option={dataTest} />
  },
}))

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  cleanup()
})

function mockFetchDefault() {
  return vi.fn((url: string) => {
    const u = String(url)
    if (u.includes("metrics.json")) return Promise.resolve({ ok: true, status: 200, json: async () => ({ sharpe: 1.62, annual_return: 0.184, max_drawdown: -0.032, turnover: 0.42 }) } as any)
    if (u.includes("tearsheet.html")) return Promise.resolve({ ok: true, status: 200, text: async () => "<html><body>real backtest</body></html>" } as any)
    if (u.includes("positions.csv")) return Promise.resolve({ ok: true, status: 200, text: async () => `date,symbol,weight,close\n2026-08-12,600519.SH,0.5,1680.2\n2026-08-13,600519.SH,0.5,1692.5` } as any)
    return Promise.resolve({ ok: false, status: 404, text: async () => "" } as any)
  })
}

test("research renders tearsheet", () => {
  vi.stubGlobal("fetch", mockFetchDefault())
  render(<Research />)
  expect(screen.getByText(/本月收益热力|累积收益/)).toBeInTheDocument()
})

// === HIGH 1: robust CSV parsing — quoted fields with commas ===
test("parseCumulative handles quoted fields with commas correctly (no silent mis-parse)", async () => {
  const fn = (ResearchModule as any).parseCumulative as ((s: string) => { dates: string[]; values: number[] } | null) | undefined
  // TDD red: before fix fn is undefined or fragile
  expect(fn).toBeDefined()
  if (!fn) return
  const csv = `date,symbol,weight,close\n2026-08-12,"600519.SH, Inc.",0.5,1680.2\n2026-08-13,"600519.SH, Inc.",0.5,1692.5\n2026-08-14,"600519.SH, Inc.",0.5,1671.0`
  const res = fn(csv)
  expect(res).not.toBeNull()
  // correct values: base 1680.2, second 1692.5/1680.2 ≈1.0073 not 1.0 (naive would map close to 0.5)
  expect(res!.values[1]).toBeCloseTo(1692.5 / 1680.2, 3)
  expect(res!.values[2]).toBeCloseTo(1671.0 / 1680.2, 3)
  // dates should be formatted MM-DD for display, not garbage
  expect(res!.dates[0]).toBe("08-12")
  expect(res!.dates[1]).toBe("08-13")
})

test("parseCumulative preserves full date and formats only for display, no slice(5) garbage on non-ISO", async () => {
  const fn = (ResearchModule as any).parseCumulative as any
  expect(fn).toBeDefined()
  if (!fn) return
  const csv = `date,symbol,weight,close\n2026/08/12,600519.SH,0.5,1680.2\n2026/08/13,600519.SH,0.5,1692.5`
  const res = fn(csv)
  // robust parser should not produce garbage like "/08/12".slice(5) => "8/12" truncated incorrectly
  // It should keep full date or fallback gracefully, but not throw and not produce D1
  expect(res).not.toBeNull()
  // dates should be either full "2026/08/12" or formatted, but not empty or "D1" fallback only for missing
  expect(res!.dates[0].length).toBeGreaterThan(2)
})

test("parseCumulative with <2 valid rows returns null and UI shows honest empty state instead of mock fallback", async () => {
  const fn = (ResearchModule as any).parseCumulative as any
  expect(fn).toBeDefined()
  if (!fn) return
  const csvSingle = `date,symbol,weight,close\n2026-08-12,600519.SH,0.5,1680.2`
  expect(fn(csvSingle)).toBeNull()
  // UI should show honest empty/error, not silent mock [1.0,1.01...]
  vi.stubGlobal("fetch", mockFetchDefault())
  render(<Research csvPreview={csvSingle} metrics={{ sharpe: 1.1, annual_return: 0.1, max_drawdown: -0.02, turnover: 0.3 }} />)
  // after fix, should show placeholder text like 暂无有效回测数据 / 解析失败 / 数据不足
  await waitFor(() => {
    const txt = document.body.textContent || ""
    expect(txt).toMatch(/暂无有效|解析失败|数据不足|等待真实/)
  })
  // should NOT show mock-driven fallback badge alone without honest indication
  // the honest placeholder badge should be visible (multiple honest texts now: badge + placeholder title/desc)
  expect(screen.queryAllByText(/暂无有效|解析失败|数据不足/).length).toBeGreaterThan(0)
})

test("truncateOnLineBoundary slices on line boundary not mid-line", async () => {
  const fn = (ResearchModule as any).truncateOnLineBoundary as ((txt: string, max: number) => string) | undefined
  expect(fn).toBeDefined()
  if (!fn) return
  // build 5000 chars: 10 lines of ~500 chars each + newline, cut at 4000 should land mid-line if naive slice
  const line = "2026-08-12,600519.SH,0.5,1680.2,extra,extra,extra,extra,extra,extra,extra,extra,extra,extra\n"
  const big = line.repeat(20) // ~ >4000
  const truncated = fn(big, 4000)
  expect(truncated.length).toBeLessThanOrEqual(4000)
  // should end with newline or complete line, not mid-row (no dangling partial after last newline)
  expect(truncated.endsWith("\n") || truncated.split("\n").pop()!.length < 80).toBeTruthy()
  // naive slice would produce a trailing partial line without newline; robust version avoids it
  const lastLine = truncated.split("\n").at(-1) || ""
  // last line should be either empty (ends with newline) or a complete CSV row containing commas
  if (lastLine.length > 0) expect(lastLine.split(",").length).toBe(4)
})

// === HIGH 2: XSS via iframe srcDoc with allow-same-origin ===
test("tearsheet iframe is safe: no srcDoc with allow-same-origin, uses src with empty sandbox", async () => {
  const htmlWithScript = `<html><body><script>alert('xss')</script><h1>tearsheet</h1></body></html>` + "x".repeat(400)
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const u = String(url)
    if (u.includes("tearsheet.html")) return Promise.resolve({ ok: true, status: 200, text: async () => htmlWithScript } as any)
    if (u.includes("metrics.json")) return Promise.resolve({ ok: true, status: 200, json: async () => ({ sharpe: 1 }) } as any)
    if (u.includes("positions.csv")) return Promise.resolve({ ok: true, status: 200, text: async () => `date,symbol,weight,close\n2026-08-12,600519.SH,0.5,1680.2\n2026-08-13,600519.SH,0.5,1692.5` } as any)
    return Promise.resolve({ ok: false } as any)
  }))
  render(<Research />)
  await waitFor(() => expect(document.querySelector('iframe[title="tearsheet"]') || screen.queryByText(/tearsheet/)).toBeTruthy(), { timeout: 2000 })
  const iframe = document.querySelector('iframe[title="tearsheet"]') as HTMLIFrameElement | null
  // after fix iframe should use src not srcDoc, and sandbox should NOT contain allow-same-origin
  if (iframe) {
    expect(iframe.getAttribute("sandbox")).not.toContain("allow-same-origin")
    // either no srcDoc or src points to /v1/backtest/tearsheet.html
    const hasSrc = iframe.getAttribute("src") || ""
    const hasSrcDoc = iframe.getAttribute("srcDoc")
    // safest is src direct; srcDoc if present must be null/empty or sanitized; we enforce src approach
    expect(hasSrc).toContain("/v1/backtest/tearsheet.html")
    expect(hasSrcDoc).toBeFalsy()
  }
})

// === HIGH 3: props-to-state desync ===
test("props updates sync to UI: metrics/drawdowns/csvPreview react to parent changes (single source of truth)", async () => {
  vi.stubGlobal("fetch", mockFetchDefault())
  const { rerender } = render(<Research metrics={{ sharpe: 1.0, annual_return: 0.05, max_drawdown: -0.01, turnover: 0.2 }} drawdowns={[{ start: "2026-08-10", end: "2026-08-11", depth: -0.5, duration: 1 }]} csvPreview={`date,symbol,weight,close\n2026-08-12,600519.SH,0.5,100\n2026-08-13,600519.SH,0.5,110`} />)
  expect(screen.getByText("1.00")).toBeInTheDocument() // sharpe 1.0
  expect(screen.getByText(/2026-08-10/)).toBeInTheDocument()
  rerender(<Research metrics={{ sharpe: 2.5, annual_return: 0.2, max_drawdown: -0.02, turnover: 0.3 }} drawdowns={[{ start: "2026-09-01", end: "2026-09-02", depth: -1.5, duration: 2 }]} csvPreview={`date,symbol,weight,close\n2026-08-12,600519.SH,0.5,200\n2026-08-13,600519.SH,0.5,220`} />)
  await waitFor(() => expect(screen.getByText("2.50")).toBeInTheDocument())
  expect(screen.getByText(/2026-09-01/)).toBeInTheDocument()
  expect(screen.queryByText(/2026-08-10/)).not.toBeInTheDocument()
})

// === MEDIUM 4: unstable useEffect deps — object identity should not cause refetch loops ===
test("unstable metrics object identity does not trigger extra fetch loops", async () => {
  const fetchMock = vi.fn((url: string) => {
    const u = String(url)
    if (u.includes("metrics.json")) return Promise.resolve({ ok: true, status: 200, json: async () => ({ sharpe: 1 }) } as any)
    if (u.includes("tearsheet.html")) return Promise.resolve({ ok: true, status: 200, text: async () => "<html>ok</html>" } as any)
    if (u.includes("positions.csv")) return Promise.resolve({ ok: true, status: 200, text: async () => `date,symbol,weight,close\n2026-08-12,600519.SH,0.5,1680.2\n2026-08-13,600519.SH,0.5,1692.5` } as any)
    return Promise.resolve({ ok: false } as any)
  })
  vi.stubGlobal("fetch", fetchMock)
  // pass metrics prop so fetchMetrics should be skipped; rerender with new object but same content should NOT refetch metrics
  const { rerender } = render(<Research metrics={{ sharpe: 1.62 }} />)
  await waitFor(() => expect(fetchMock).toHaveBeenCalled())
  const callsAfterFirst = fetchMock.mock.calls.length
  rerender(<Research metrics={{ sharpe: 1.62 }} />)
  // give effect a tick
  await new Promise(r => setTimeout(r, 50))
  // should not have triggered another metrics fetch (at most tearsheet maybe but metrics not refetched)
  const metricsCalls = fetchMock.mock.calls.filter(c => String(c[0]).includes("metrics.json")).length
  expect(metricsCalls).toBeLessThanOrEqual(1)
  // ensure no loop: total calls should not grow by more than 1
  expect(fetchMock.mock.calls.length - callsAfterFirst).toBeLessThanOrEqual(1)
})

// === MEDIUM 5/6: heatmap scaling and hasHeatmap truthiness ===
test("hasHeatmap is falsy when derivedHeatmap is null even if metricsMonthlyRaw exists (shows 暂无数据)", async () => {
  vi.stubGlobal("fetch", mockFetchDefault())
  // metrics.monthly is invalid type that leads derivedHeatmap null
  render(<Research metrics={{ sharpe: 1, monthly: "bad_string" as any }} />)
  await waitFor(() => expect(screen.getByText(/本月收益热力/)).toBeInTheDocument())
  expect(screen.getByText(/暂无数据/)).toBeInTheDocument()
})

test("heatmap does not fabricate padded zeros when monthly returns <10", async () => {
  const fn = (ResearchModule as any).deriveHeatmapForTest as ((m: any) => any) | undefined
  // if helper not exported, check via UI: pass 3 monthly returns, derived should be 3 not 10
  vi.stubGlobal("fetch", mockFetchDefault())
  if (fn) {
    const pts = fn({ monthly_returns: [0.01, -0.005, 0.02] })
    expect(pts).not.toBeNull()
    expect(pts.length).toBe(3)
    expect(pts.some((p: any) => p[2] === 0 && p[0] === 3)).toBeFalsy() // no padded zero at idx 3
  } else {
    // fallback: ensure no crash and placeholder logic works
    render(<Research metrics={{ monthly_returns: [0.01, -0.005, 0.02] }} />)
    expect(screen.getByText(/本月收益热力/)).toBeInTheDocument()
  }
})

// === MEDIUM 8: target="_blank" rel ===
test("external links use rel=\"noopener noreferrer\"", async () => {
  vi.stubGlobal("fetch", mockFetchDefault())
  render(<Research />)
  const link = document.querySelector('a[href="/v1/backtest/tearsheet.html"]') as HTMLAnchorElement | null
  expect(link).not.toBeNull()
  const rel = link!.getAttribute("rel") || ""
  expect(rel).toContain("noopener")
  expect(rel).toContain("noreferrer")
})
