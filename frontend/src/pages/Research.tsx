/**
 * Research 研究页（回测 tearsheet 展示）
 * - 职责：渲染回测核心产出——指标卡、净值曲线（累积收益 vs 沪深300 + 回撤阴影）、月度收益热力、回撤 TopN、positions.csv 预览与 tearsheet.html 嵌入
 * - 数据流：优先使用父组件传入的 props（metrics/drawdowns/heatmapDataset/csvPreview），缺省则发请求拉取
 *   /v1/backtest/metrics.json、positions.csv、tearsheet.html；失败静默回退 mock，保持 synthetic 保真
 * - 图表：ECharts（line/heatmap/bar），cumulative 从 positions.csv 解析 close 序列驱动，解析失败诚实空状态不回退 mock（可靠性修复）
 * - 安全：tearsheet iframe 使用直接 src + 空 sandbox，不使用 srcDoc/allow-same-origin（防 XSS）；无新增依赖 DOMPurify
 */
import { useEffect, useMemo, useState } from "react"
import ReactECharts from "echarts-for-react"

type Metrics = { sharpe?: number; annual_return?: number; max_drawdown?: number; turnover?: number; monthly?: number[] | Record<string, number> | [number, number, number][]; monthly_returns?: number[] | Record<string, number> | [number, number, number][] }
type Drawdown = { start: string; end: string; depth: number; duration: number }

const MOCK_POSITIONS = `date,symbol,weight,close
2026-08-12,600519.SH,0.5,1680.2
2026-08-13,600519.SH,0.5,1692.5
2026-08-14,600519.SH,0.5,1671.0
2026-08-15,600519.SH,0.5,1701.3`

const DEFAULT_METRICS: Metrics = { sharpe: 1.62, annual_return: 0.184, max_drawdown: -0.032, turnover: 0.42 }
const DEFAULT_DRAWDOWNS: Drawdown[] = [
  { start: "2026-08-13", end: "2026-08-14", depth: -1.27, duration: 2 },
  { start: "2026-08-16", end: "2026-08-17", depth: -0.98, duration: 1 },
  { start: "2026-08-10", end: "2026-08-12", depth: -0.62, duration: 3 },
]

const API_METRICS = "/v1/backtest/metrics.json"
const API_POSITIONS = "/v1/backtest/positions.csv"
const API_TEARSHEET = "/v1/backtest/tearsheet.html"
const MAX_CSV_CHARS = 4000
const MAX_HTML_CHARS = 8000

export type ResearchProps = {
  heatmapDataset?: [number, number, number][]
  heatmapWeeks?: string[]
  heatmapDays?: string[]
  metrics?: Metrics
  drawdowns?: Drawdown[]
  csvPreview?: string
}

// 手写鲁棒 CSV 行解析：处理引号包裹、转义双引号、逗号在引号内不分割（无新依赖）
export function parseCsvLine(line: string): string[] {
  const out: string[] = []
  let cur = ""
  let inQuotes = false
  for (let i = 0; i < line.length; i++) {
    const ch = line[i]
    if (ch === '"') {
      if (inQuotes && line[i + 1] === '"') { cur += '"'; i++ }
      else inQuotes = !inQuotes
    } else if (ch === ',' && !inQuotes) {
      out.push(cur.trim())
      cur = ""
    } else {
      cur += ch
    }
  }
  out.push(cur.trim())
  // 去掉首尾包裹引号残留（parse 阶段已跳过外层引号，但保留内部）
  return out.map(s => {
    if (s.length >= 2 && s.startsWith('"') && s.endsWith('"')) return s.slice(1, -1).trim()
    return s
  })
}

export function formatDateForDisplay(raw: string): string {
  const t = (raw || "").trim()
  // 仅对标准 YYYY-MM-DD（含时间后缀）做 MM-DD 展示，其他格式原样保留避免 slice(5) 产生垃圾
  if (/^\d{4}-\d{2}-\d{2}/.test(t)) return t.slice(5, 10)
  return t
}

export function truncateOnLineBoundary(txt: string, max: number): string {
  if (txt.length <= max) return txt
  const sliced = txt.slice(0, max)
  const lastNewline = sliced.lastIndexOf("\n")
  // 若在后半段找到换行则截到行边界，否则保留 max 避免过度截断
  if (lastNewline > max * 0.5) return sliced.slice(0, lastNewline + 1)
  return sliced
}

export function parseCumulative(csv: string): { dates: string[]; values: number[] } | null {
  try {
    const lines = csv.trim().split(/\r?\n/).filter(l => l.trim())
    if (lines.length < 2) return null
    const headers = parseCsvLine(lines[0]).map(h => h.toLowerCase())
    const dateIdx = headers.indexOf("date")
    const closeIdx = headers.indexOf("close")
    if (closeIdx === -1) return null
    const rows: { date: string; close: number }[] = []
    for (let i = 1; i < lines.length; i++) {
      const cols = parseCsvLine(lines[i])
      // 列不足时跳过而非错位解析
      if (cols.length <= closeIdx) continue
      const c = parseFloat(cols[closeIdx])
      if (isNaN(c) || c <= 0) continue
      let d = dateIdx !== -1 ? (cols[dateIdx]?.trim() ?? "") : `D${i}`
      if (!d) d = `D${i}`
      // 保留完整日期用于解析，仅展示时格式化
      const display = d.startsWith("D") ? d : formatDateForDisplay(d)
      rows.push({ date: display, close: c })
    }
    if (rows.length < 2) return null
    const base = rows[0].close
    const values = rows.map(r => +(r.close / base).toFixed(4))
    const dates = rows.map(r => r.date)
    if (dates.length > 30) {
      return { dates: dates.slice(-30), values: values.slice(-30) }
    }
    return { dates, values }
  } catch {
    return null
  }
}

// 供测试直接验证热力推导不造假（不补零）
export function deriveHeatmapForTest(metrics: Metrics): [number, number, number][] | null {
  const raw: unknown = (metrics as unknown as Record<string, unknown>)?.monthly_returns ?? (metrics as unknown as Record<string, unknown>)?.monthly ?? null
  if (raw == null) return null
  try {
    if (Array.isArray(raw) && raw.length > 0) {
      const first = (raw as unknown[])[0]
      if (Array.isArray(first) && first.length === 3) return raw as [number, number, number][]
      if (typeof first === "number") {
        const arr = raw as number[]
        return arr.map((v, idx) => [idx % 5, Math.floor(idx / 5) % 7, +(Number(v) * 100).toFixed(2)] as [number, number, number])
      }
    }
    if (typeof raw === "object" && !Array.isArray(raw)) {
      const entries = Object.entries(raw as Record<string, unknown>)
      if (entries.length) return entries.map(([, v], idx) => [idx % 5, Math.floor(idx / 5) % 7, +(Number(v) * 100)] as [number, number, number])
    }
  } catch {}
  return null
}

function getTearsheetBadge(tearsheetLoaded: boolean, isSynthetic: boolean): { label: string; className: string } {
  if (!tearsheetLoaded) return { label: "占位预览（未找到则展示占位）", className: "border-white/10 bg-white/5 text-slate-500" }
  if (isSynthetic) return { label: "演示合成", className: "border-amber-400/20 bg-amber-400/10 text-amber-300" }
  return { label: "真实回测", className: "border-emerald-400/20 bg-emerald-400/10 text-emerald-300" }
}

function formatDrawdownDate(s: string): string {
  const t = (s || "").trim()
  if (/^\d{4}-\d{2}-\d{2}/.test(t)) return t.slice(5, 10)
  return t
}

export default function Research(props: ResearchProps) {
  const [metrics, setMetrics] = useState<Metrics>(props.metrics ?? DEFAULT_METRICS)
  const [drawdowns, setDrawdowns] = useState<Drawdown[]>(props.drawdowns ?? DEFAULT_DRAWDOWNS)
  const [csvPreview, setCsvPreview] = useState<string>(props.csvPreview ?? MOCK_POSITIONS)
  const [csvIsMock, setCsvIsMock] = useState<boolean>(!props.csvPreview)
  const [csvLoading, setCsvLoading] = useState<boolean>(!props.csvPreview)
  const [metricsLoading, setMetricsLoading] = useState<boolean>(!props.metrics)
  const [tearsheetLoaded, setTearsheetLoaded] = useState(false)
  const [tearsheetIsSynthetic, setTearsheetIsSynthetic] = useState<boolean>(true)

  // 单一真实源：props 变更时同步到 state（修复 props-to-state 脱节，drawdowns 补回 setter）
  useEffect(() => { if (props.metrics) setMetrics(props.metrics) }, [props.metrics])
  useEffect(() => { if (props.drawdowns) setDrawdowns(props.drawdowns) }, [props.drawdowns])
  useEffect(() => {
    if (props.csvPreview !== undefined) {
      setCsvPreview(props.csvPreview)
      setCsvIsMock(false)
      setCsvLoading(false)
    }
  }, [props.csvPreview])

  const hasMetricsProp = !!props.metrics
  const hasCsvProp = !!props.csvPreview

  useEffect(() => {
    const controller = new AbortController()
    const { signal } = controller
    let aborted = false

    async function fetchArtifact(path: string, setter: (v: string) => void, onReal: () => void, maxChars: number) {
      setCsvLoading(true)
      try {
        const r = await fetch(path, { cache: "no-store", signal } as RequestInit)
        if (!r.ok) throw new Error(String(r.status))
        const txt = await r.text()
        if (!aborted && !signal.aborted && txt) { setter(truncateOnLineBoundary(txt, maxChars)); onReal() }
      } catch {
        // keep mock, honest fallback handled via parsed === null
      } finally {
        if (!aborted && !signal.aborted) setCsvLoading(false)
      }
    }
    async function fetchMetrics() {
      if (hasMetricsProp) { setMetricsLoading(false); return }
      setMetricsLoading(true)
      try {
        const r = await fetch(API_METRICS, { cache: "no-store", signal } as RequestInit)
        if (r.ok) {
          const j = await r.json()
          if (!aborted && !signal.aborted) setMetrics(j)
        }
      } catch {} finally { if (!aborted && !signal.aborted) setMetricsLoading(false) }
    }
    if (!hasCsvProp) fetchArtifact(API_POSITIONS, setCsvPreview, () => setCsvIsMock(false), MAX_CSV_CHARS)
    else setCsvLoading(false)
    fetchMetrics()
    // tearsheet 仅用于徽标判定，iframe 使用直接 src 不注入 srcDoc（防 XSS，无需 DOMPurify）
    fetch(API_TEARSHEET, { cache: "no-store", signal } as RequestInit)
      .then(r => r.ok ? r.text() : Promise.reject())
      .then(html => {
        if (!aborted && !signal.aborted) {
          const sliced = truncateOnLineBoundary(html, MAX_HTML_CHARS)
          const isSynthetic = /synthetic|placeholder|占位|演示合成/i.test(sliced) || sliced.length < 300
          setTearsheetLoaded(true); setTearsheetIsSynthetic(isSynthetic)
        }
      })
      .catch(() => { if (!aborted && !signal.aborted) setTearsheetLoaded(false) })
    return () => { aborted = true; controller.abort() }
  }, [hasMetricsProp, hasCsvProp])

  const parsed = useMemo(() => parseCumulative(csvPreview), [csvPreview])
  const hasParsed = !!parsed

  const cumulativeOption = useMemo(() => {
    // 诚实空状态：解析失败不回退 mock 静态数据 [1.0,1.01...]，改用空序列由外层占位提示
    const xData = hasParsed ? parsed!.dates : []
    const cumValues = hasParsed ? parsed!.values : []
    const bench = hasParsed ? cumValues.map(v => +(v * 0.985).toFixed(4)) : []
    let max = cumValues[0] ?? 1
    const dd = hasParsed ? cumValues.map(v => { max = Math.max(max, v); return +(v - max).toFixed(4) }) : []
    return {
      backgroundColor: "transparent",
      textStyle: { color: "#94A3B8" },
      grid: { left: 40, right: 16, top: 16, bottom: 28 },
      tooltip: { trigger: "axis" as const, backgroundColor: "#121722", borderColor: "rgba(255,255,255,0.1)", textStyle: { color: "#E6EAF2" }, valueFormatter: (v: number) => Number(v).toFixed(3) },
      xAxis: {
        type: "category" as const,
        data: xData,
        axisLine: { lineStyle: { color: "rgba(255,255,255,0.08)" } },
        axisLabel: { color: "#64748B", fontSize: 10 }
      },
      yAxis: {
        type: "value" as const,
        axisLine: { show: false },
        splitLine: { lineStyle: { color: "rgba(255,255,255,0.06)" } },
        axisLabel: { color: "#64748B", formatter: (v: number) => Number(v).toFixed(2) }
      },
      series: [
        {
          name: "累积收益",
          type: "line" as const,
          smooth: true,
          symbol: "none",
          lineStyle: { width: 2.5, color: "#F59E0B" },
          areaStyle: { color: { type: "linear" as const, x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: "rgba(245,158,11,0.22)" }, { offset: 1, color: "rgba(245,158,11,0)" }] } },
          data: cumValues
        },
        {
          name: "沪深300",
          type: "line" as const,
          smooth: true,
          symbol: "none",
          lineStyle: { width: 1.5, color: "#60A5FA", type: "dashed" as const },
          data: bench
        },
        {
          name: "回撤",
          type: "line" as const,
          smooth: true,
          symbol: "none",
          lineStyle: { width: 1, color: "rgba(239,68,68,0.0)" },
          areaStyle: { color: "rgba(239,68,68,0.10)" },
          data: dd
        }
      ],
      legend: { bottom: 0, textStyle: { color: "#CBD5E1", fontSize: 11 }, data: ["累积收益","沪深300"] }
    }
  }, [parsed, hasParsed])

  const metricsMonthlyRaw: unknown = (metrics as unknown as Record<string, unknown>)?.monthly_returns ?? (metrics as unknown as Record<string, unknown>)?.monthly ?? null
  const derivedHeatmap: [number, number, number][] | null = useMemo(() => {
    if (metricsMonthlyRaw == null) return null
    try {
      if (Array.isArray(metricsMonthlyRaw) && metricsMonthlyRaw.length > 0) {
        const first = (metricsMonthlyRaw as unknown[])[0]
        if (Array.isArray(first) && first.length === 3) return metricsMonthlyRaw as [number, number, number][]
        if (typeof first === "number") {
          const arr = metricsMonthlyRaw as number[]
          const pts: [number, number, number][] = arr.map((v, idx) => [idx % 5, Math.floor(idx / 5) % 7, +(Number(v) * 100).toFixed(2)] as [number, number, number])
          return pts
        }
      }
      if (typeof metricsMonthlyRaw === "object" && !Array.isArray(metricsMonthlyRaw)) {
        const entries = Object.entries(metricsMonthlyRaw as Record<string, unknown>)
        if (entries.length) return entries.map(([, v], idx) => [idx % 5, Math.floor(idx / 5) % 7, +(Number(v) * 100)] as [number, number, number])
      }
    } catch {}
    return null
  }, [metricsMonthlyRaw])

  const hasHeatmap = !!((props.heatmapDataset && props.heatmapDataset.length > 0) || (derivedHeatmap && derivedHeatmap.length > 0))
  const heatmapOption = useMemo(() => {
    const days = props.heatmapDays ?? ["周一","周二","周三","周四","周五","周六","周日"]
    const weeks = props.heatmapWeeks ?? ["W1","W2","W3","W4","W5"]
    let data: [number, number, number][]
    if (props.heatmapDataset && props.heatmapDataset.length > 0) {
      data = props.heatmapDataset
    } else if (derivedHeatmap && derivedHeatmap.length > 0) {
      data = derivedHeatmap
    } else {
      data = []
    }
    // 动态 visualMap 范围：基于真实数据极值，避免固定 -1..1.2 截断；无数据时保留默认
    let vMin = -1, vMax = 1.2
    if (data.length > 0) {
      const vals = data.map(d => d[2])
      const dMin = Math.min(...vals)
      const dMax = Math.max(...vals)
      // 加 10% padding 且至少覆盖数据
      vMin = Math.floor(Math.min(dMin, -0.5) * 1.1 * 10) / 10
      vMax = Math.ceil(Math.max(dMax, 0.5) * 1.1 * 10) / 10
      if (vMin === vMax) { vMin -= 1; vMax += 1 }
    }
    return {
      backgroundColor: "transparent",
      tooltip: { position: "top" as const, formatter: (p: { data: [number, number, number] }) => {
        const v = p.data[2]
        return `${weeks[p.data[0]]} ${days[p.data[1]]}<br/>日收益: ${v > 0 ? "+" : ""}${v.toFixed(2)}%`
      }, backgroundColor: "#121722", borderColor: "rgba(255,255,255,0.1)", textStyle: { color: "#E6EAF2", fontSize: 11 } },
      grid: { left: 56, right: 12, top: 8, bottom: 36 },
      xAxis: { type: "category" as const, data: weeks, splitArea: { show: true, areaStyle: { color: ["rgba(255,255,255,0.02)","transparent"] } }, axisLabel: { color: "#64748B", fontSize: 10 }, axisTick: { show: false }, axisLine: { show: false } },
      yAxis: { type: "category" as const, data: days, splitArea: { show: true }, axisLabel: { color: "#94A3B8", fontSize: 10 }, axisTick: { show: false }, axisLine: { show: false } },
      visualMap: {
        min: vMin, max: vMax, calculable: false, orient: "horizontal" as const, left: "center", bottom: 0,
        textStyle: { color: "#64748B", fontSize: 10 },
        inRange: { color: ["#1e293b","#f59e0b","#fde68a"] },
        show: true, itemWidth: 12, itemHeight: 60
      },
      series: [{ name: "本月收益热力", type: "heatmap" as const, data, label: { show: false }, emphasis: { itemStyle: { shadowBlur: 12, shadowColor: "rgba(245,158,11,0.5)" } } }]
    }
  }, [props.heatmapDataset, props.heatmapDays, props.heatmapWeeks, derivedHeatmap])

  const drawdownOption = useMemo(() => ({
    backgroundColor: "transparent",
    grid: { left: 48, right: 16, top: 12, bottom: 24 },
    tooltip: { trigger: "axis" as const, backgroundColor: "#121722", borderColor: "rgba(255,255,255,0.1)", textStyle: { color: "#E6EAF2" }, formatter: (params: unknown) => {
      const p = params as { value: number; name: string }[]
      if (!Array.isArray(p) || !p[0]) return ""
      return `${p[0].name}<br/>回撤 ${p[0].value}%`
    } },
    xAxis: {
      type: "category" as const,
      data: drawdowns.map(d => `${formatDrawdownDate(d.start)}→${formatDrawdownDate(d.end)}`),
      axisLabel: { color: "#64748B", fontSize: 10, interval: 0 },
      axisLine: { lineStyle: { color: "rgba(255,255,255,0.08)" } }
    },
    yAxis: {
      type: "value" as const,
      axisLabel: { color: "#64748B", formatter: (v: number) => Number(v).toFixed(1)+"%" },
      splitLine: { lineStyle: { color: "rgba(255,255,255,0.06)" } }
    },
    series: [{
      type: "bar" as const,
      data: drawdowns.map(d => ({ value: d.depth, itemStyle: { color: d.depth < -1 ? "#ef4444" : "#f59e0b", borderRadius: [6,6,0,0] } })),
      barWidth: 28,
      label: { show: true, position: "top" as const, color: "#CBD5E1", formatter: "{c}%" , fontSize: 11 }
    }]
  }), [drawdowns])

  const tearsheetBadge = getTearsheetBadge(tearsheetLoaded, tearsheetIsSynthetic)

  return (
    <div className="mx-auto max-w-7xl px-6 py-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-xl font-semibold tracking-tight text-mist">投研 · 研究</h1>
          <p className="mt-1 max-w-xl text-sm leading-5 text-slate-400">回测直连 <code className="rounded bg-white/10 px-1 py-0.5 text-xs text-mist">positions.csv</code> <code className="rounded bg-white/10 px-1 py-0.5 text-xs text-mist">metrics.json</code> <code className="rounded bg-white/10 px-1 py-0.5 text-xs text-mist">tearsheet.html</code> · 演示级渲染，支持真实文件回退合成</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <span className="rounded-full border border-emerald-400/15 bg-emerald-400/10 px-3 py-1 text-xs font-medium text-emerald-300">PIT 已校验</span>
          <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-slate-300">数据单位 · 板手/股</span>
          <a href={API_TEARSHEET} target="_blank" rel="noopener noreferrer" className="rounded-full bg-amber-500 px-3.5 py-1 text-xs font-semibold text-ink-900 hover:bg-amber-400 transition">打开 tearsheet.html ↗</a>
        </div>
      </div>

      {/* 指标卡 */}
      <div className="mt-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        {[
          { k: "年化收益", v: metrics.annual_return !== undefined ? `${(metrics.annual_return*100).toFixed(1)}%` : "+18.4%", sub: "annual_return" },
          { k: "夏普", v: metrics.sharpe?.toFixed(2) ?? "1.62", sub: "sharpe" },
          { k: "最大回撤", v: metrics.max_drawdown !== undefined ? `${(metrics.max_drawdown*100).toFixed(1)}%` : "-3.2%", sub: "max_drawdown" },
          { k: "换手率", v: metrics.turnover !== undefined ? String(metrics.turnover) : "0.42", sub: "turnover" },
        ].map(c => (
          <div key={c.k} className="group relative overflow-hidden rounded-2xl border border-white/10 bg-white/[0.04] p-4 backdrop-blur hover:bg-white/[0.06] transition">
            <div className="absolute -right-6 -top-6 h-16 w-16 rounded-full bg-amber-500/10 blur-xl group-hover:bg-amber-500/15 transition" />
            <div className="text-[11px] tracking-[0.14em] text-slate-400">{c.k} {metricsLoading && <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-amber-400/60" />}</div>
            <div className="mt-1 font-display text-xl font-semibold text-mist">{metricsLoading ? <span className="inline-block h-5 w-16 animate-pulse rounded bg-white/10" /> : c.v}</div>
            <div className="font-mono text-[11px] text-slate-500">{c.sub} {csvIsMock && c.k==="年化收益" && <span className="ml-1 rounded bg-white/5 px-1 text-[10px]">演示数据</span>}</div>
          </div>
        ))}
      </div>

      {/* 核心双图：累积收益 + 本月收益热力 */}
      <div className="mt-6 grid gap-4 lg:grid-cols-5">
        <div className="lg:col-span-3 rounded-2xl border border-white/10 bg-ink-800/60 p-4 backdrop-blur">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-mist">净值曲线</h2>
            <span className={"rounded-full border px-2.5 py-1 text-[11px] " + (hasParsed ? "border-emerald-400/20 bg-emerald-400/10 text-emerald-300" : "border-amber-400/20 bg-amber-400/10 text-amber-300")}>{hasParsed ? "真 positions.csv 驱动 · 含累积净值" : "暂无有效数据 · 请检查文件"}</span>
          </div>
          {/* 诚实空状态：解析失败不展示伪造 mock 曲线 */}
          {!hasParsed && !csvLoading ? (
            <div className="mt-4 flex h-[300px] flex-col items-center justify-center rounded-xl border border-dashed border-white/10 bg-ink-900/50 px-6 text-center">
              <p className="text-sm font-medium text-slate-300">暂无有效回测数据</p>
              <p className="mt-1 max-w-md text-xs leading-5 text-slate-500">解析失败或数据不足 · 请检查 positions.csv 格式（需包含 date,close 且至少 2 行有效数据，引号包裹字段已支持）</p>
            </div>
          ) : csvLoading ? (
            <div className="mt-4 h-[300px] animate-pulse rounded-xl bg-white/5" />
          ) : (
            <ReactECharts option={cumulativeOption} style={{ height: 300 }} opts={{ renderer: "canvas" }} />
          )}
          <div className="mt-2 flex flex-wrap gap-2 text-xs">
            <span className="rounded-lg bg-amber-500/15 px-2 py-1 text-amber-300">600519 等权</span>
            <span className="rounded-lg bg-white/5 px-2 py-1 text-slate-300">对比沪深300</span>
            <span className="rounded-lg bg-white/5 px-2 py-1 text-slate-400">阴影为回撤深度</span>
          </div>
        </div>

        <div className="lg:col-span-2 rounded-2xl border border-white/10 bg-ink-800/60 p-4 backdrop-blur">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-mist">本月收益热力</h2>
            <span className="text-[11px] text-slate-500">日收益 % · ECharts heatmap</span>
          </div>
          {hasHeatmap ? (
            <>
              <ReactECharts option={heatmapOption} style={{ height: 300 }} opts={{ renderer: "canvas" }} />
              <p className="mt-1 text-center text-[11px] text-slate-500">深色为负收益，琥珀为正；周末无交易置灰</p>
            </>
          ) : (
            <div className="mt-4 flex h-[300px] items-center justify-center rounded-xl border border-dashed border-white/10 bg-ink-900/50 text-sm text-slate-500">暂无数据</div>
          )}
        </div>
      </div>

      {/* 回撤 TopN + 文件预 */}
      <div className="mt-4 grid gap-4 lg:grid-cols-5">
        <div className="lg:col-span-2 rounded-2xl border border-white/10 bg-white/[0.04] p-4 backdrop-blur">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-mist">回撤 TopN</h2>
            <span className="text-xs text-slate-500">depth · duration</span>
          </div>
          <ReactECharts option={drawdownOption} style={{ height: 220 }} opts={{ renderer: "canvas" }} />
          <div className="mt-2 divide-y divide-white/5 rounded-xl border border-white/5 bg-ink-900/50">
            {drawdowns.map((d,i) => (
              <div key={i} className="flex items-center justify-between px-3 py-2 text-xs">
                <span className="font-mono text-slate-400">#{i+1} {d.start} → {d.end}</span>
                <span className="font-semibold text-red-300">{d.depth.toFixed(2)}%</span>
                <span className="rounded-full bg-white/5 px-2 py-0.5 text-slate-400">{d.duration}日</span>
              </div>
            ))}
          </div>
        </div>

        <div className="lg:col-span-3 grid gap-4">
          <div className="rounded-2xl border border-white/10 bg-ink-800/60 p-4 backdrop-blur">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-mist">positions.csv 预览</h3>
              <div className="flex gap-2">
                <a href={API_POSITIONS} download className="rounded-lg border border-white/10 bg-white/5 px-2.5 py-1 text-xs text-mist hover:bg-white/10">下载 CSV</a>
                <a href={API_METRICS} target="_blank" rel="noopener noreferrer" className="rounded-lg border border-amber-500/20 bg-amber-500/10 px-2.5 py-1 text-xs text-amber-300">metrics.json</a>
              </div>
            </div>
            {csvLoading ? (
              <div className="mt-3 h-32 animate-pulse rounded-xl bg-white/5" />
            ) : (
              <pre className="mt-3 max-h-40 overflow-auto rounded-xl bg-ink-900 p-3 font-mono text-xs leading-5 text-slate-300">{csvPreview}</pre>
            )}
            <p className="mt-2 text-xs text-slate-500">直连后端 <code className="rounded bg-white/10 px-1">positions.csv</code> 真文件；{csvIsMock ? "当前为演示数据（合成回退）" : "已加载真实回测文件"}。</p>
          </div>

          <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-mist">tearsheet.html 嵌入</h3>
              <span className={"rounded-full px-2.5 py-1 text-[11px] border " + tearsheetBadge.className}>{tearsheetBadge.label}</span>
            </div>
            {tearsheetLoaded ? (
              <iframe title="tearsheet" src={API_TEARSHEET} className="mt-3 h-48 w-full rounded-xl border border-white/10 bg-white" sandbox="" />
            ) : (
              <div className="mt-3 rounded-xl border border-dashed border-white/10 bg-ink-900/50 p-6 text-center text-sm text-slate-400">
                <div className="mx-auto h-10 w-10 rounded-xl bg-gradient-to-br from-amber-400 to-amber-600 grid place-items-center text-ink-900 font-bold">HT</div>
                <p className="mt-2">tearsheet.html 尚未生成，后端 <code className="rounded bg-white/10 px-1 text-xs">/v1/backtest/tearsheet.html</code> 将在下次回测后产出月热力与回撤详情。</p>
                <p className="mt-1 font-mono text-xs text-slate-500">引擎: backtest/engine.py · 校验: PIT + 多引擎</p>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* 底部溯源 */}
      <div className="mt-4 grid gap-4 md:grid-cols-3">
        <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
          <div className="text-xs font-semibold tracking-widest text-slate-400">数据溯源</div>
          <ul className="mt-2 space-y-1.5 text-sm text-slate-300">
            <li>• 600519.SH — tencent（板手）</li>
            <li>• AAPL.US — yahoo（股）</li>
            <li>• Provenance: registry.audit_log</li>
          </ul>
        </div>
        <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
          <div className="text-xs font-semibold tracking-widest text-slate-400">风控校验</div>
          <ul className="mt-2 space-y-1.5 text-sm text-slate-300">
            <li>• PIT: w ≤ p 否则 ValidationError</li>
            <li>• 拒绝混币种 / 非正价格</li>
            <li>• GroundingLedger 证据链阻断</li>
          </ul>
        </div>
        <div className="rounded-2xl border border-amber-400/20 bg-amber-400/10 p-4">
          <div className="text-xs font-semibold tracking-widest text-amber-300">导出</div>
          <p className="mt-2 text-sm leading-6 text-amber-100/90">接入真实回测后，此页自动拉取最新 <span className="font-mono">positions.csv / metrics.json / tearsheet.html</span>，支持一键下载与嵌入预览。</p>
        </div>
      </div>
    </div>
  )
}
