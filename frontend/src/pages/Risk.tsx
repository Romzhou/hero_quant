/**
 * Risk 风控页
 * - 职责：展示敞口/限额/熔断/拒单等风控总览、规则清单（PIT/cross_source/双桶熔断/grounding）与归因五类覆盖
 * - 数据流：GET /v1/risk/summary 拉真实指标（turnover/cross_source/pit/circuit），失败回退静态占位；所有决策以 ledger.verify() 为可追溯锚点
 */
import { useEffect, useState } from "react"

type RiskSummary = {
  turnover?: number
  cross_source?: string
  pit?: string
  circuit?: string
  exposure?: number
  single_limit?: number
  circuit_threshold?: number
  reject_rate?: number
}

// --- constants extracted (no visual change) ---
const API_RISK_SUMMARY = "/v1/risk/summary"
const ROUTES = { RESEARCH: "/research" } as const
const FALLBACK = {
  EXPOSURE: "62%",
  SINGLE_LIMIT: "20%",
  CIRCUIT_THRESHOLD: "80%",
  CIRCUIT_LABEL: "50%",
} as const

function isFiniteNumber(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v)
}

function sanitizeRiskSummary(raw: unknown): RiskSummary | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null
  const r = raw as Record<string, unknown>
  const out: RiskSummary = {}
  if (isFiniteNumber(r.exposure)) out.exposure = r.exposure
  if (isFiniteNumber(r.single_limit)) out.single_limit = r.single_limit
  if (isFiniteNumber(r.circuit_threshold)) out.circuit_threshold = r.circuit_threshold
  if (isFiniteNumber(r.turnover)) out.turnover = r.turnover
  if (isFiniteNumber(r.reject_rate)) out.reject_rate = r.reject_rate
  if (typeof r.cross_source === "string") out.cross_source = r.cross_source
  if (typeof r.pit === "string") out.pit = r.pit
  if (typeof r.circuit === "string") out.circuit = r.circuit
  return out
}

export default function Risk() {
  const [summary, setSummary] = useState<RiskSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    let aborted = false
    async function load() {
      setLoading(true)
      setError(null)
      try {
        const r = await fetch(API_RISK_SUMMARY, { cache: "no-store" })
        if (!r.ok) throw new Error(String(r.status))
        const j = await r.json()
        if (aborted) return
        const sanitized = sanitizeRiskSummary(j)
        // sanitized may be empty object when all fields invalid -> still a valid response, render "--"
        // only null means completely unparseable (array/null) -> treat as error shape but keep fallback via null
        if (sanitized === null) {
          // keep null to trigger degraded badge? but payload was object-like failure: treat as empty?
          // if json was null/array we keep null; UI will show placeholders but badge degraded only if error
          // To keep honesty, if sanitized null and response was object, show empty placeholder object not degraded
          setSummary(null)
        } else {
          setSummary(sanitized)
        }
      } catch (e) {
        if (!aborted) {
          setError(e instanceof Error ? e.message : String(e))
          setSummary(null)
        }
      } finally {
        if (!aborted) setLoading(false)
      }
    }
    load()
    return () => {
      aborted = true
    }
  }, [reloadKey])

  const turnover = summary?.turnover
  const crossSource = summary?.cross_source
  const pit = summary?.pit
  const circuit = summary?.circuit

  const isDegraded = !loading && (error !== null || summary === null)
  const badge = (() => {
    if (loading) return { text: "加载中…", cls: "border-white/10 bg-white/5 text-slate-300" }
    if (isDegraded) return { text: "数据异常 · --", cls: "border-amber-500/30 bg-amber-500/10 text-amber-200" }
    if (circuit) return { text: `风控正常 · ${circuit}`, cls: "border-emerald-400/20 bg-emerald-400/10 text-emerald-300" }
    return { text: "风控正常 · CLOSED", cls: "border-emerald-400/20 bg-emerald-400/10 text-emerald-300" }
  })()

  // guarded display values — invalid fields render "--" not NaN%, fetch failure keeps placeholder
  const exposureDisplay = loading
    ? "…"
    : summary === null
      ? FALLBACK.EXPOSURE
      : isFiniteNumber(summary.exposure)
        ? `${(summary.exposure * 100).toFixed(0)}%`
        : "--"

  const singleLimitDisplay = loading
    ? "…"
    : summary === null
      ? FALLBACK.SINGLE_LIMIT
      : isFiniteNumber(summary.single_limit)
        ? `${(summary.single_limit * 100).toFixed(0)}%`
        : "--"

  const circuitDisplay = loading ? "…" : typeof circuit === "string" && circuit.length > 0 ? String(circuit) : "未触发"

  const circuitThresholdSub = (() => {
    if (loading) return "阈值 …"
    if (summary === null) return `阈值 ${FALLBACK.CIRCUIT_THRESHOLD}`
    if (isFiniteNumber(summary.circuit_threshold)) return `阈值 ${(summary.circuit_threshold * 100).toFixed(0)}%`
    return "阈值 --"
  })()

  const turnoverDisplay = loading
    ? "…"
    : summary === null
      ? "0.42"
      : isFiniteNumber(turnover)
        ? String(turnover)
        : "--"

  const turnoverSub = crossSource ? `cross_source ${crossSource}` : "PIT/证据链"

  // For the lower sections, use guarded helpers to avoid NaN%
  const pitText = loading ? "…" : typeof pit === "string" && pit.length > 0 ? String(pit) : "w ≤ p 否则 ValidationError"
  const crossSourceText = loading ? "…" : typeof crossSource === "string" && crossSource.length > 0 ? String(crossSource) : "首bar偏差>1% 阻断"
  const circuitDualText = loading ? "…" : typeof circuit === "string" && circuit.length > 0 ? String(circuit) : FALLBACK.CIRCUIT_LABEL
  const groundingText = (() => {
    if (isFiniteNumber(turnover)) return `turnover ${turnover}`
    if (summary !== null && turnover !== undefined) return "turnover --"
    return "证据链未命中阻断"
  })()

  return (
    <div className="mx-auto max-w-7xl px-6 py-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-xl font-semibold text-mist">Risk · 风控</h1>
          <p className="mt-1 text-sm text-slate-400">敞口 · 熔断 · 归因 · 证据链 · ShadowAccount</p>
        </div>
        <span
          className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium ${badge.cls}`}
        >
          {isDegraded && <span className="h-1.5 w-1.5 rounded-full bg-amber-400 animate-pulse" aria-hidden />}
          {badge.text}
        </span>
      </div>

      {error && !loading && (
        <div
          role="alert"
          className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3"
        >
          <div className="flex items-center gap-2 text-sm text-amber-200">
            <span className="h-2 w-2 rounded-full bg-amber-400 animate-pulse" aria-hidden />
            风控数据获取失败，当前显示为占位数据
          </div>
          <button
            type="button"
            onClick={() => setReloadKey((k) => k + 1)}
            className="rounded-full border border-amber-500/30 bg-amber-500/20 px-3 py-1 text-xs font-medium text-amber-100 hover:bg-amber-500/30"
          >
            重试
          </button>
        </div>
      )}

      <div className="mt-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        {[
          { k: "总敞口", v: loading ? "…" : exposureDisplay, sub: "杠杆 1.2x" },
          { k: "单票上限", v: loading ? "…" : singleLimitDisplay, sub: "600519 18%" },
          { k: "日内熔断", v: circuitDisplay, sub: circuitThresholdSub },
          { k: "换手率", v: turnoverDisplay, sub: turnoverSub },
        ].map((c) => (
          <div key={c.k} className="rounded-2xl border border-white/10 bg-white/[0.04] p-4 backdrop-blur">
            <div className="text-[11px] tracking-[0.14em] text-slate-400">{c.k}</div>
            <div className="mt-1 font-display text-xl font-semibold text-mist">
              {loading ? <span className="inline-block h-5 w-16 animate-pulse rounded bg-white/10" /> : c.v}
            </div>
            <div className="font-mono text-[11px] text-slate-500">{c.sub}</div>
          </div>
        ))}
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <div className="rounded-2xl border border-white/10 bg-ink-800/60 p-4 backdrop-blur">
          <h2 className="text-sm font-semibold text-mist">风控规则 · 3-5条</h2>
          <div className="mt-3 space-y-2 text-xs">
            <div className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2">
              <span className="text-slate-400">PIT 校验</span>
              <span className="text-emerald-300">{pitText}</span>
            </div>
            <div className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2">
              <span className="text-slate-400">cross_source 1%</span>
              <span className="text-emerald-300">{crossSourceText}</span>
            </div>
            <div className="flex justify-between rounded-xl bg-amber-500/10 border border-amber-500/20 px-3 py-2">
              <span className="text-amber-300">熔断双桶</span>
              <span className="font-mono text-amber-100">Circuit {circuitDualText} / OTel 80%</span>
            </div>
            <div className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2">
              <span className="text-slate-400">Grounding</span>
              <span className="text-mist">{groundingText}</span>
            </div>
          </div>
        </div>

        <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-mist">归因 · 5类 coverage&gt;0</h2>
            <a href={ROUTES.RESEARCH} className="rounded-full bg-white/5 px-2.5 py-1 text-[11px] text-slate-300 hover:bg-white/10">
              查看研究 → tearsheet
            </a>
          </div>
          <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
            {[
              { k: "择时", v: "+1.2%" },
              { k: "选股", v: "+0.8%" },
              { k: "风控", v: "-0.1%" },
              { k: "成本", v: "-0.4%" },
              { k: "其他", v: "+0.2%" },
            ].map((x) => (
              <div key={x.k} className="rounded-xl border border-white/5 bg-white/[0.03] p-3 text-center">
                <div className="text-slate-400">{x.k}</div>
                <div className="mt-1 font-semibold text-mist">{x.v}</div>
              </div>
            ))}
          </div>
          <p className="mt-3 text-xs leading-5 text-slate-500">归因5类均有覆盖，coverage 100%；ShadowAccount 2.0 对账日跑。</p>
        </div>
      </div>

      <div className="mt-4 rounded-2xl border border-amber-400/20 bg-amber-400/10 p-4">
        <div className="flex items-center justify-between">
          <div className="text-xs font-semibold tracking-widest text-amber-300">审计 · 可追溯</div>
          <a href={ROUTES.RESEARCH} className="rounded-full bg-white px-2.5 py-1 text-xs font-semibold text-amber-700 hover:bg-white/90">
            打开研究页 ↗
          </a>
        </div>
        <p className="mt-2 text-sm leading-6 text-amber-100/90">每笔风控决策均可回溯验证，超阈值自动进入备用路径并记录全链路。</p>
        <p className="mt-1 font-mono text-xs text-amber-200/60">
          ledger.verify() · trace.jsonl{isFiniteNumber(turnover) ? ` · turnover ${turnover}` : ""}
        </p>
      </div>
    </div>
  )
}
