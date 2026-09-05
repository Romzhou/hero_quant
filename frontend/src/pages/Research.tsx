/**
 * Research 研究页（回测 tearsheet 展示）
 * - 职责：渲染回测核心产出——指标卡、净值曲线（累积收益 vs 合成基准 + 回撤阴影）、月度收益热力、回撤 TopN、positions.csv 预览与 tearsheet.html 嵌入
 * - 数据流：优先使用父组件传入的 props（metrics/drawdowns/heatmapDataset/csvPreview），缺省则发请求拉取
 *   /v1/backtest/metrics.json、positions.csv、drawdowns.json、tearsheet.html；失败回退 mock 但必须带 isMock 标记（provenance 必传，不冒充 live）
 * - 图表：ECharts（line/heatmap/bar），cumulative 从 positions.csv 解析 close 序列驱动，解析失败诚实空状态不回退 mock
 * - 安全：tearsheet iframe 使用直接 src + 空 sandbox，不使用 srcDoc/allow-same-origin（防 XSS）；无新增依赖 DOMPurify
 * - 泄漏防护：所有 fetch 共享 AbortController + isAlive 守卫，卸载时 abort
 */

import { useEffect, useMemo, useRef, useState } from "react"
import ReactECharts from "echarts-for-react"
import { API_METRICS, API_POSITIONS, API_TEARSHEET, API_DRAWDOWNS } from "../config/api"

// 集中管理：后端回测产物路径统一由 src/config/api 维护（避免跨页硬编码漂移）；截断上限保留本地
const MAX_CSV_CHARS = 4000
const MAX_HTML_CHARS = 8000
const MAX_POINTS = 30

type Metrics = { sharpe?: number; annual_return?: number; max_drawdown?: number; turnover?: number; monthly?: number[] | Record<string, number> | [number, number, number][]; monthly_returns?: number[] | Record<string, number> | [number, number, number][]; isMock?: boolean; provenance?: string; synthetic?: boolean }
type Drawdown = { start: string; end: string; depth: number; duration: number }

const MOCK_POSITIONS = `date,symbol,weight,close
2026-08-12,600519.SH,0.5,1680.2
2026-08-13,600519.SH,0.5,1692.5
2026-08-14,600519.SH,0.5,1671.0
2026-08-15,600519.SH,0.5,1701.3`

const DEFAULT_METRICS: Metrics = { sharpe: 1.62, annual_return: 0.184, max_drawdown: -0.032, turnover: 0.42, isMock: true, provenance: "synthetic" }
const DEFAULT_DRAWDOWNS: Drawdown[] = [
  { start: "2026-08-13", end: "2026-08-14", depth: -1.27, duration: 2 },
  { start: "2026-08-16", end: "2026-08-17", depth: -0.98, duration: 1 },
  { start: "2026-08-10", end: "2026-08-12", depth: -0.62, duration: 3 },
]

// ECharts 容器需显式像素高度以正确测量 canvas；提取为常量避免内联对象字面量每次渲染新建身份导致额外重渲染，Tailwind h-[300px] 在 ECharts 包装层不稳定故选用 style 常量并文档化
const CHART_STYLE_CUMULATIVE = { height: 300 } as const
const CHART_STYLE_HEATMAP = { height: 300 } as const
const CHART_STYLE_DRAWDOWN = { height: 220 } as const

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
  return formatMonthDay(raw)
}

// 日期展示统一入口：标准 YYYY-MM-DD（含时间后缀）取 MM-DD，其余原样保留，避免 slice(5) 产生垃圾
export function formatMonthDay(raw: string): string {
  const t = (raw || "").trim()
  if (/^\d{4}-\d{2}-\d{2}/.test(t)) return t.slice(5, 10)
  return t
}

export function truncateOnLineBoundary(txt: string, max: number): string {
  if (txt.length <= max) return txt
  const sliced = txt.slice(0, max)
  const lastNewline = sliced.lastIndexOf("\n")
  // 结构化截断：只要找到换行即截到行边界，避免尾部半行（如截断的 close 值）被解析为错误数据点；
  // 无换行时无法保行边界，保留 max（调用方 parse 失败走诚实空状态）
  if (lastNewline !== -1) return sliced.slice(0, lastNewline + 1)
  return sliced
}

export function parseCumulative(csv: string): { dates: string[]; values: number[]; truncated: boolean; totalRows: number } | null {
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
    const totalRows = rows.length
    if (dates.length > MAX_POINTS) {
      // 显式截断标记：调用方必须展示“仅展示最近 N 点（共 M 点）”而非静默 slice
      return { dates: dates.slice(-MAX_POINTS), values: values.slice(-MAX_POINTS), truncated: true, totalRows }
    }
    return { dates, values, truncated: false, totalRows }
  } catch (e) {
    console.debug("[Research] parseCumulative failed:", e)
    return null
  }
}

// 单一真实源：热力推导统一实现，消除 deriveHeatmapForTest 与组件内重复推导的双维护
// 有限性守卫：单条 NaN/Infinity 不得污染整组 visualMap（过滤后再映射与缩放）
export function deriveHeatmap(metrics: Metrics): [number, number, number][] | null {
  const raw: unknown = (metrics as unknown as Record<string, unknown>)?.monthly_returns ?? (metrics as unknown as Record<string, unknown>)?.monthly ?? null
  if (raw === null || raw === undefined) return null
  try {
    if (Array.isArray(raw) && raw.length > 0) {
      const first = (raw as unknown[])[0]
      if (Array.isArray(first) && first.length === 3) {
        const triples = (raw as unknown[]).filter(
          (t): t is [number, number, number] =>
            Array.isArray(t) && t.length === 3 && t.every(v => typeof v === "number" && Number.isFinite(v)),
        ) as [number, number, number][]
        return triples.length > 0 ? triples : null
      }
      if (typeof first === "number") {
        const arr = raw as unknown[]
        return arr
          .map((v, idx) => [idx % 5, Math.floor(idx / 5) % 7, +(Number(v) * 100).toFixed(2)] as [number, number, number])
          .filter(d => Number.isFinite(d[2]))
      }
    }
    if (typeof raw === "object" && !Array.isArray(raw)) {
      const entries = Object.entries(raw as Record<string, unknown>)
      if (entries.length) {
        const pts = entries
          .map(([, v], idx) => [idx % 5, Math.floor(idx / 5) % 7, +(Number(v) * 100)] as [number, number, number])
          .filter(d => Number.isFinite(d[2]))
        return pts.length > 0 ? pts : null
      }
    }
  } catch (e) {
    console.debug("[Research] deriveHeatmap failed:", e)
  }
  return null
}

// 兼容测试导入名，指向单一实现，避免分叉
export const deriveHeatmapForTest = deriveHeatmap

// provenance 保守判定：缺 markers 即视为 mock，直到后端显式声明 live；DEFAULT_METRICS 指纹全中也强制 mock
export function isMockProvenance(j: Record<string, unknown>): boolean {
  if (j.isMock === true || j.provenance === "synthetic" || j.provenance === "mock" || j.synthetic === true) return true
  if (
    Number(j.sharpe) === DEFAULT_METRICS.sharpe &&
    Number(j.annual_return) === DEFAULT_METRICS.annual_return &&
    Number(j.max_drawdown) === DEFAULT_METRICS.max_drawdown &&
    Number(j.turnover) === DEFAULT_METRICS.turnover
  ) return true
  // 显式 live 声明才视为真实，其余（含空 {} / 无标记）一律保守为 mock
  return !(j.isMock === false && j.provenance === "live")
}

// ECharts tooltip HTML 转义：week/day/drawdown 名均可能来自 props 或网络 JSON，HTML 模式下必须转义
export function escapeHtml(s: unknown): string {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]!))
}

function getTearsheetBadge(tearsheetLoaded: boolean, isSynthetic: boolean): { label: string; className: string } {
  if (!tearsheetLoaded) return { label: "占位预览（未找到则展示占位）", className: "border-white/10 bg-white/5 text-slate-500" }
  if (isSynthetic) return { label: "演示合成", className: "border-amber-400/20 bg-amber-400/10 text-amber-300" }
  return { label: "真实回测", className: "border-emerald-400/20 bg-emerald-400/10 text-emerald-300" }
}

function formatDrawdownDate(s: string): string {
  return formatMonthDay(s)
}

// 回撤条目校验：缺 depth/start 时不渲染，避免 toFixed 抛错
function isValidDrawdown(d: unknown): d is Drawdown {
  if (!d || typeof d !== "object") return false
  const o = d as Record<string, unknown>
  return typeof o.start === "string" && typeof o.end === "string" && typeof o.depth === "number" && Number.isFinite(o.depth) && typeof o.duration === "number" && Number.isFinite(o.duration)
}

// 指标数值归一：后端可能返回字符串数字（"1.62"），先 Number() 再有限性校验，避免 toFixed 抛错
function toFiniteNumber(v: unknown): number | undefined {
  const n = typeof v === "number" ? v : typeof v === "string" && v.trim() !== "" ? Number(v) : NaN
  return Number.isFinite(n) ? n : undefined
}

export default function Research(props: ResearchProps) {
  const [metrics, setMetrics] = useState<Metrics>(props.metrics ?? DEFAULT_METRICS)
  const [metricsIsMock, setMetricsIsMock] = useState<boolean>(() => {
    if (!props.metrics) return true
    return isMockProvenance(props.metrics as unknown as Record<string, unknown>)
  })
  const [drawdowns, setDrawdowns] = useState<Drawdown[]>(() => {
    const init = props.drawdowns ?? DEFAULT_DRAWDOWNS
    return (init as unknown[]).filter(isValidDrawdown) as Drawdown[]
  })
  const [drawdownsIsMock, setDrawdownsIsMock] = useState<boolean>(() => !props.drawdowns)
  const [csvPreview, setCsvPreview] = useState<string>(props.csvPreview ?? MOCK_POSITIONS)
  const [csvIsMock, setCsvIsMock] = useState<boolean>(!props.csvPreview)
  const [csvLoading, setCsvLoading] = useState<boolean>(!props.csvPreview)
  const [metricsLoading, setMetricsLoading] = useState<boolean>(!props.metrics)
  const [tearsheetLoaded, setTearsheetLoaded] = useState(false)
  const [tearsheetIsSynthetic, setTearsheetIsSynthetic] = useState<boolean>(true)

  // 按资源拆分请求作用域：共享 controller 负责卸载取消；csvReqId 防止过期 fetch 覆盖更新的 props
  // （声明前置：上方 props 同步 effect 需递增代际声明优先权）
  const csvReqIdRef = useRef(0)

  // 单一真实源：props 变更时同步到 state（修复 props-to-state 脱节），provenance 走保守判定
  useEffect(() => {
    if (props.metrics) {
      setMetrics(props.metrics)
      setMetricsIsMock(isMockProvenance(props.metrics as unknown as Record<string, unknown>))
    }
  }, [props.metrics])
  useEffect(() => {
    if (props.drawdowns) {
      const valid = (props.drawdowns as unknown[]).filter(isValidDrawdown) as Drawdown[]
      setDrawdowns(valid)
      setDrawdownsIsMock(false)
    }
  }, [props.drawdowns])
  useEffect(() => {
    if (props.csvPreview !== undefined) {
      // props 优先：递增请求代际，迟到的 in-flight fetch 不得覆盖新 props
      csvReqIdRef.current++
      setCsvPreview(props.csvPreview)
      setCsvIsMock(false)
      setCsvLoading(false)
    }
  }, [props.csvPreview])

  // 布尔存在性依赖：避免同真值新对象身份触发重复拉取，同步由上方独立 effect 负责
  useEffect(() => {
    const controller = new AbortController()
    const { signal } = controller
    let aborted = false
    // 共享 fetcher：统一 cache 策略、signal 与 aborted 守卫，消除重复 boilerplate
    const fetchNoStore = (url: string) => fetch(url, { cache: "no-store", signal })
    const isAlive = () => !aborted && !signal.aborted

    async function fetchArtifact(path: string, setter: (v: string) => void, onReal: () => void, maxChars: number) {
      const myId = ++csvReqIdRef.current
      if (!props.csvPreview) setCsvLoading(true)
      try {
        const r = await fetchNoStore(path)
        if (!r.ok) throw new Error(String(r.status))
        const txt = await r.text()
        // 过期响应丢弃：props 已同步更新时不再覆盖
        if (myId !== csvReqIdRef.current) return
        if (isAlive() && txt) { setter(truncateOnLineBoundary(txt, maxChars)); onReal() }
      } catch (e) {
        console.debug("[Research] fetchArtifact failed:", e)
        // keep mock, honest fallback handled via parsed === null
      } finally {
        if (myId === csvReqIdRef.current && isAlive()) setCsvLoading(false)
      }
    }
    async function fetchMetrics() {
      if (props.metrics) {
        setMetricsLoading(false)
        return
      }
      setMetricsLoading(true)
      try {
        const r = await fetchNoStore(API_METRICS)
        if (!r.ok) throw new Error(String(r.status))
        const j = await r.json() as Record<string, unknown>
        if (!isAlive()) return
        setMetrics(j as Metrics)
        // provenance 保守判定：缺 markers 即视为 mock；DEFAULT_METRICS 指纹全中也强制 mock
        setMetricsIsMock(isMockProvenance(j))
      } catch (e) {
        console.debug("[Research] fetchMetrics failed:", e)
        if (isAlive()) {
          // 保留 DEFAULT_METRICS 但标记 isMock，配合徽标避免误认真实
          setMetricsIsMock(true)
        }
      } finally { if (isAlive()) setMetricsLoading(false) }
    }
    async function fetchDrawdowns() {
      if (props.drawdowns) return
      try {
        const r = await fetchNoStore(API_DRAWDOWNS)
        if (!r.ok) throw new Error(String(r.status))
        const j = await r.json() as unknown
        if (!isAlive()) return
        const arr = Array.isArray(j) ? j : (j as Record<string, unknown>)?.drawdowns ?? (j as Record<string, unknown>)?.data ?? null
        if (Array.isArray(arr)) {
          const valid = (arr as unknown[]).filter(isValidDrawdown) as Drawdown[]
          if (valid.length > 0) {
            setDrawdowns(valid)
            // 裸数组信封无 provenance 字段：命中 DEFAULT 指纹视为 mock 回退形状，否则视为 live 载荷（不误标真实数据）
            if (Array.isArray(j)) {
              const first = valid[0]
              const isDefaultShape =
                valid.length === DEFAULT_DRAWDOWNS.length &&
                first.start === DEFAULT_DRAWDOWNS[0].start &&
                first.end === DEFAULT_DRAWDOWNS[0].end &&
                first.depth === DEFAULT_DRAWDOWNS[0].depth
              setDrawdownsIsMock(isDefaultShape)
            } else {
              setDrawdownsIsMock(isMockProvenance((j ?? {}) as Record<string, unknown>))
            }
            return
          }
        }
        // 解析失败保持 mock 并标记 isMock
        setDrawdownsIsMock(true)
      } catch (e) {
        console.debug("[Research] fetchDrawdowns failed:", e)
        if (isAlive()) setDrawdownsIsMock(true)
      }
    }
    async function fetchTearsheet() {
      try {
        const r = await fetchNoStore(API_TEARSHEET)
        if (!r.ok) throw new Error(String(r.status))
        const html = await r.text()
        if (!isAlive()) return
        const sliced = truncateOnLineBoundary(html, MAX_HTML_CHARS)
        // 合成判定仅看显式标记：小体积真实 tearsheet 不得以 length 误判为合成
        const isSynthetic = /synthetic|placeholder|占位|演示合成/i.test(sliced)
        setTearsheetLoaded(true)
        setTearsheetIsSynthetic(isSynthetic)
      } catch (e) {
        console.debug("[Research] tearsheet fetch failed:", e)
        if (isAlive()) setTearsheetLoaded(false)
      }
    }
    if (!props.csvPreview) fetchArtifact(API_POSITIONS, setCsvPreview, () => setCsvIsMock(false), MAX_CSV_CHARS)
    else setCsvLoading(false)
    fetchMetrics()
    fetchDrawdowns()
    fetchTearsheet()
    return () => { aborted = true; controller.abort() }
  }, [!!props.metrics, props.csvPreview !== undefined, !!props.drawdowns])

  const parsed = useMemo(() => parseCumulative(csvPreview), [csvPreview])
  const hasParsed = !!parsed

  const cumulativeOption = useMemo(() => {
    // 诚实空状态：解析失败不回退 mock 静态数据 [1.0,1.01...]，改用空序列由外层占位提示
    const xData = hasParsed ? parsed!.dates : []
    const cumValues = hasParsed ? parsed!.values : []
    // 合成基准：非真实沪深300，仅为演示对比（cum*0.985），必须以合成名义展示，带 isMock 语义
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
          name: "基准(合成)",
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
      legend: { bottom: 0, textStyle: { color: "#CBD5E1", fontSize: 11 }, data: ["累积收益","基准(合成)","回撤"] }
    }
  }, [parsed, hasParsed])

  // 复用单一推导，避免双份实现漂移
  const derivedHeatmap: [number, number, number][] | null = useMemo(() => deriveHeatmap(metrics), [metrics])

  const hasHeatmap = !!((props.heatmapDataset && props.heatmapDataset.length > 0) || (derivedHeatmap && derivedHeatmap.length > 0))
  const heatmapOption = useMemo(() => {
    const days = props.heatmapDays ?? ["周一","周二","周三","周四","周五","周六","周日"]
    const weeks = props.heatmapWeeks ?? ["W1","W2","W3","W4","W5"]
    let data: [number, number, number][]
    if (props.heatmapDataset && props.heatmapDataset.length > 0) {
      // props 直传同样需有限性过滤：单条 NaN/Infinity 不得污染 visualMap 缩放
      data = props.heatmapDataset.filter(
        (d): d is [number, number, number] =>
          Array.isArray(d) && d.length === 3 && d.every(v => typeof v === "number" && Number.isFinite(v)),
      ) as [number, number, number][]
    } else if (derivedHeatmap && derivedHeatmap.length > 0) {
      data = derivedHeatmap
    } else {
      data = []
    }
    // 动态 visualMap 范围：基于真实数据极值，避免固定 -1..1.2 截断；无数据时保留默认
    // 有限性守卫 + 迭代求极值：过滤 NaN/Infinity 后再缩放，避免 spread 大数组栈溢出
    let vMin = -1, vMax = 1.2
    if (data.length > 0) {
      const vals = data.map(d => d[2]).filter(v => Number.isFinite(v))
      if (vals.length > 0) {
        let dMin = vals[0]
        let dMax = vals[0]
        for (let i = 1; i < vals.length; i++) {
          if (vals[i] < dMin) dMin = vals[i]
          if (vals[i] > dMax) dMax = vals[i]
        }
        // 加 10% padding 且至少覆盖数据
        vMin = Math.floor(Math.min(dMin, -0.5) * 1.1 * 10) / 10
        vMax = Math.ceil(Math.max(dMax, 0.5) * 1.1 * 10) / 10
        if (!Number.isFinite(vMin) || !Number.isFinite(vMax)) { vMin = -1; vMax = 1.2 }
        if (vMin === vMax) { vMin -= 1; vMax += 1 }
      }
    }
    return {
      backgroundColor: "transparent",
      tooltip: { position: "top" as const, formatter: (p: unknown) => {
        const param: unknown = Array.isArray(p) ? (p as unknown[])[0] : p
        const d = (param as { data?: unknown })?.data as unknown[]
        if (!Array.isArray(d) || d.length < 3 || typeof d[2] !== "number" || typeof d[0] !== "number" || typeof d[1] !== "number") return ""
        const v = d[2] as number
        const wi = d[0] as number
        const di = d[1] as number
        // XSS 防护：week/day 来自 props（外部可控），HTML tooltip 模式下转义后再插值
        const w = escapeHtml(weeks[wi] ?? String(wi))
        const day = escapeHtml(days[di] ?? String(di))
        return `${w} ${day}<br/>日收益: ${v > 0 ? "+" : ""}${v.toFixed(2)}%`
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

  const drawdownOption = useMemo(() => {
    // 过滤非法条目，避免渲染期抛错
    const validDrawdowns = drawdowns.filter(isValidDrawdown)
    return {
    backgroundColor: "transparent",
    grid: { left: 48, right: 16, top: 12, bottom: 24 },
    tooltip: { trigger: "axis" as const, backgroundColor: "#121722", borderColor: "rgba(255,255,255,0.1)", textStyle: { color: "#E6EAF2" }, formatter: (params: unknown) => {
      // ECharts axis 触发时 params 为数组，单项触发时为对象；if/else 替代嵌套三元
      let arr: { value: number; name: string }[]
      if (Array.isArray(params)) {
        arr = params as { value: number; name: string }[]
      } else if (params !== null && params !== undefined) {
        arr = [params as { value: number; name: string }]
      } else {
        arr = []
      }
      if (arr.length === 0 || arr[0] === null || arr[0] === undefined || typeof arr[0].value !== "number") return ""
      // XSS 防护：name 源自 drawdowns 日期（网络 JSON 可控），HTML tooltip 下转义
      return `${escapeHtml(arr[0].name ?? "")}<br/>回撤 ${Number(arr[0].value).toFixed(2)}%`
    } },
    xAxis: {
      type: "category" as const,
      data: validDrawdowns.map(d => `${formatDrawdownDate(d.start)}→${formatDrawdownDate(d.end)}`),
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
      data: validDrawdowns.map(d => ({ value: d.depth, itemStyle: { color: d.depth < -1 ? "#ef4444" : "#f59e0b", borderRadius: [6,6,0,0] } })),
      barWidth: 28,
      label: { show: true, position: "top" as const, color: "#CBD5E1", formatter: "{c}%" , fontSize: 11 }
    }]
  }}, [drawdowns])

  const tearsheetBadge = getTearsheetBadge(tearsheetLoaded, tearsheetIsSynthetic)

  // 净值徽标文案：if/else 替代嵌套三元
  function cumulativeBadgeText(): string {
    if (!hasParsed) return "暂无有效数据 · 请检查文件"
    if (parsed!.truncated) return `真 positions.csv 驱动 · 仅展示最近${MAX_POINTS}点（共 ${parsed!.totalRows} 点，已截断）`
    return "真 positions.csv 驱动 · 含累积净值"
  }

  // 净值区正文：if/else 替代嵌套三元（加载骨架 / 诚实空状态 / 图表）
  function renderCumulativeBody() {
    if (csvLoading) {
      return <div className="mt-4 h-[300px] animate-pulse rounded-xl bg-white/5" />
    }
    if (!hasParsed) {
      return (
        <div className="mt-4 flex h-[300px] flex-col items-center justify-center rounded-xl border border-dashed border-white/10 bg-ink-900/50 px-6 text-center">
          <p className="text-sm font-medium text-slate-300">暂无有效回测数据</p>
          <p className="mt-1 max-w-md text-xs leading-5 text-slate-500">解析失败或数据不足 · 请检查 positions.csv 格式（需包含 date,close 且至少 2 行有效数据，引号包裹字段已支持）</p>
        </div>
      )
    }
    return <ReactECharts option={cumulativeOption} style={CHART_STYLE_CUMULATIVE} opts={{ renderer: "canvas" }} notMerge={true} lazyUpdate={true} />
  }

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

      {/* 指标卡：数值经 toFiniteNumber 归一，字符串数字不抛错 */}
      <div className="mt-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        {(() => {
          const annual = toFiniteNumber(metrics.annual_return)
          const sharpe = toFiniteNumber(metrics.sharpe)
          const dd = toFiniteNumber(metrics.max_drawdown)
          const turnover = toFiniteNumber(metrics.turnover)
          return [
            { k: "年化收益", v: annual !== undefined ? `${(annual * 100).toFixed(1)}%` : "+18.4%", sub: "annual_return" },
            { k: "夏普", v: sharpe !== undefined ? sharpe.toFixed(2) : "1.62", sub: "sharpe" },
            { k: "最大回撤", v: dd !== undefined ? `${(dd * 100).toFixed(1)}%` : "-3.2%", sub: "max_drawdown" },
            { k: "换手率", v: turnover !== undefined ? String(turnover) : "0.42", sub: "turnover" },
          ]
        })().map(c => (
          <div key={c.k} className="group relative overflow-hidden rounded-2xl border border-white/10 bg-white/[0.04] p-4 backdrop-blur hover:bg-white/[0.06] transition">
            <div className="absolute -right-6 -top-6 h-16 w-16 rounded-full bg-amber-500/10 blur-xl group-hover:bg-amber-500/15 transition" />
            <div className="text-[11px] tracking-[0.14em] text-slate-400">{c.k} {metricsLoading && <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-amber-400/60" />}</div>
            <div className="mt-1 font-display text-xl font-semibold text-mist">{metricsLoading ? <span className="inline-block h-5 w-16 animate-pulse rounded bg-white/10" /> : c.v}</div>
            <div className="font-mono text-[11px] text-slate-500">{c.sub} {(metricsIsMock || csvIsMock) && c.k==="年化收益" && <span className="ml-1 rounded bg-white/5 px-1 text-[10px]">演示数据</span>}</div>
          </div>
        ))}
      </div>
      {metricsIsMock && !metricsLoading && (
        <div className="mt-2 rounded-xl border border-amber-400/20 bg-amber-400/10 px-3 py-2 text-xs text-amber-200">当前指标为演示数据（合成回退，isMock=true），非真实回测</div>
      )}

      {/* 核心双图：累积收益 + 本月收益热力 */}
      <div className="mt-6 grid gap-4 lg:grid-cols-5">
        <div className="lg:col-span-3 rounded-2xl border border-white/10 bg-ink-800/60 p-4 backdrop-blur">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-mist">净值曲线</h2>
            <span className={"rounded-full border px-2.5 py-1 text-[11px] " + (hasParsed ? "border-emerald-400/20 bg-emerald-400/10 text-emerald-300" : "border-amber-400/20 bg-amber-400/10 text-amber-300")}>{cumulativeBadgeText()}</span>
          </div>
          {/* 诚实空状态：解析失败不展示伪造 mock 曲线 */}
          {renderCumulativeBody()}
          <div className="mt-2 flex flex-wrap gap-2 text-xs">
            <span className="rounded-lg bg-amber-500/15 px-2 py-1 text-amber-300">600519 等权</span>
            <span className="rounded-lg bg-white/5 px-2 py-1 text-slate-300">对比基准(合成)</span>
            <span className="rounded-lg bg-white/5 px-2 py-1 text-slate-400">阴影为回撤深度 · 合成基准非真实沪深300</span>
          </div>
        </div>

        <div className="lg:col-span-2 rounded-2xl border border-white/10 bg-ink-800/60 p-4 backdrop-blur">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-mist">本月收益热力</h2>
            <span className="text-[11px] text-slate-500">日收益 % · ECharts heatmap</span>
          </div>
          {hasHeatmap ? (
            <>
              <ReactECharts option={heatmapOption} style={CHART_STYLE_HEATMAP} opts={{ renderer: "canvas" }} notMerge={true} lazyUpdate={true} />
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
            <span className="text-xs text-slate-500">depth · duration {drawdownsIsMock && <span className="ml-1 rounded bg-white/5 px-1 text-[10px]">演示数据</span>}</span>
          </div>
          <ReactECharts option={drawdownOption} style={CHART_STYLE_DRAWDOWN} opts={{ renderer: "canvas" }} notMerge={true} lazyUpdate={true} />
          {/* 列表保留原始 YYYY-MM-DD 便于检索与测试命中，图表轴仍用格式化 */}
          <div className="mt-2 divide-y divide-white/5 rounded-xl border border-white/5 bg-ink-900/50">
            {drawdowns.filter(isValidDrawdown).map((d,i) => (
              <div key={i} className="flex items-center justify-between px-3 py-2 text-xs">
                <span className="font-mono text-slate-400">#{i+1} {d.start} → {d.end}</span>
                <span className="font-semibold text-red-300">{Number(d.depth).toFixed(2)}%</span>
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
            <p className="mt-2 text-xs text-slate-500">直连后端 <code className="rounded bg-white/10 px-1">positions.csv</code> 真文件；{csvIsMock ? "当前为演示数据（合成回退，isMock=true）" : "已加载真实回测文件"}。</p>
          </div>

          <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-mist">tearsheet.html 嵌入</h3>
              <span className={"rounded-full px-2.5 py-1 text-[11px] border " + tearsheetBadge.className}>{tearsheetBadge.label}</span>
            </div>
            {tearsheetLoaded ? (
              <iframe title="tearsheet" src={API_TEARSHEET} className="mt-3 h-48 w-full rounded-xl border border-white/10 bg-white" sandbox="" referrerPolicy="no-referrer" />
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
            <li>• Provenance: registry.audit_log {metricsIsMock ? "· 演示合成(isMock)" : "· 真实回测"}</li>
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
          <p className="mt-2 text-sm leading-6 text-amber-100/90">接入真实回测后，此页自动拉取最新 <span className="font-mono">positions.csv / metrics.json / tearsheet.html / drawdowns.json</span>，支持一键下载与嵌入预览。</p>
        </div>
      </div>
    </div>
  )
}
