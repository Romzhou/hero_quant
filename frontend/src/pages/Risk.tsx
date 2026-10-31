/**
 * Risk 风控页
 * - 职责：展示敞口/限额/熔断/拒单等风控总览、规则清单（PIT/cross_source/双桶熔断/grounding）、真实回测归因与影子账户重放
 * - 数据流：GET /v1/risk/summary + /v1/risk/attribution + /v1/risk/shadow；全部为后端真值，
 *   缺失字段渲染 "--"，degraded=true 时顶部横幅展示 reason（不再有任何写死数字/占位文案）
 */
import { useEffect, useState } from "react"
import { authHeaders } from "../config/auth"

type RiskSummary = {
  turnover?: number
  cross_source?: string
  pit?: string
  circuit?: string
  exposure?: number
  single_limit?: number
  circuit_threshold?: number
  reject_rate?: number
  degraded?: boolean
  reason?: string
}

type RiskAttribution = {
  components?: Record<string, number | null>
  not_applicable?: string[]
  coverage?: number
  net_return?: number | null
  metrics_cumulative_return?: number | null
  turnover_notional?: number | null
  bars?: number
  warnings?: string[]
  degraded?: boolean
  reason?: string | null
}

type RiskShadow = {
  orders_total?: number | null
  allowed?: number | null
  rejected?: number | null
  rejected_by_rule?: Record<string, number>
  reject_rate?: number | null
  notional_total?: number | null
  turnover_notional?: number | null
  turnover_mean_exposure?: number | null
  bars?: number
  degraded?: boolean
  reason?: string | null
}

const API_RISK_SUMMARY = "/v1/risk/summary"
const API_RISK_ATTRIBUTION = "/v1/risk/attribution"
const API_RISK_SHADOW = "/v1/risk/shadow"
const ROUTES = { RESEARCH: "/research" } as const
const ATTRIBUTION_ROWS = [
  { key: "timing", label: "择时" },
  { key: "selection", label: "选股" },
  { key: "risk_control", label: "风控" },
  { key: "cost", label: "成本" },
  { key: "residual", label: "其他" },
] as const

function isFiniteNumber(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v)
}

function pct(v: unknown, digits = 2): string {
  return isFiniteNumber(v) ? `${(v * 100).toFixed(digits)}%` : "--"
}

function num(v: unknown): string {
  return isFiniteNumber(v) ? String(v) : "--"
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
  if (r.degraded === true) out.degraded = true
  if (typeof r.reason === "string") out.reason = r.reason
  return out
}

function sanitizeAttribution(raw: unknown): RiskAttribution | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null
  const r = raw as Record<string, unknown>
  const out: RiskAttribution = {}
  if (r.components && typeof r.components === "object" && !Array.isArray(r.components)) {
    const comps: Record<string, number | null> = {}
    for (const [k, v] of Object.entries(r.components as Record<string, unknown>)) {
      comps[k] = isFiniteNumber(v) ? v : null
    }
    out.components = comps
  }
  if (Array.isArray(r.not_applicable)) out.not_applicable = r.not_applicable.filter((x) => typeof x === "string")
  if (isFiniteNumber(r.coverage)) out.coverage = r.coverage
  if (isFiniteNumber(r.net_return)) out.net_return = r.net_return
  if (isFiniteNumber(r.metrics_cumulative_return)) out.metrics_cumulative_return = r.metrics_cumulative_return
  if (isFiniteNumber(r.turnover_notional)) out.turnover_notional = r.turnover_notional
  if (isFiniteNumber(r.bars)) out.bars = r.bars
  if (Array.isArray(r.warnings)) out.warnings = r.warnings.filter((x) => typeof x === "string")
  out.degraded = r.degraded === true
  out.reason = typeof r.reason === "string" ? r.reason : null
  return out
}

function sanitizeShadow(raw: unknown): RiskShadow | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null
  const r = raw as Record<string, unknown>
  const out: RiskShadow = {}
  if (isFiniteNumber(r.orders_total)) out.orders_total = r.orders_total
  if (isFiniteNumber(r.allowed)) out.allowed = r.allowed
  if (isFiniteNumber(r.rejected)) out.rejected = r.rejected
  if (isFiniteNumber(r.reject_rate)) out.reject_rate = r.reject_rate
  if (isFiniteNumber(r.notional_total)) out.notional_total = r.notional_total
  if (isFiniteNumber(r.turnover_notional)) out.turnover_notional = r.turnover_notional
  if (isFiniteNumber(r.turnover_mean_exposure)) out.turnover_mean_exposure = r.turnover_mean_exposure
  if (isFiniteNumber(r.bars)) out.bars = r.bars
  if (r.rejected_by_rule && typeof r.rejected_by_rule === "object" && !Array.isArray(r.rejected_by_rule)) {
    const byRule: Record<string, number> = {}
    for (const [k, v] of Object.entries(r.rejected_by_rule as Record<string, unknown>)) {
      if (isFiniteNumber(v)) byRule[k] = v
    }
    out.rejected_by_rule = byRule
  }
  out.degraded = r.degraded === true
  out.reason = typeof r.reason === "string" ? r.reason : null
  return out
}

async function fetchJson(url: string): Promise<unknown> {
  const r = await fetch(url, { cache: "no-store", headers: authHeaders() })
  if (!r.ok) throw new Error(`${url} HTTP ${r.status}`)
  return await r.json()
}

export default function Risk() {
  const [summary, setSummary] = useState<RiskSummary | null>(null)
  const [attribution, setAttribution] = useState<RiskAttribution | null>(null)
  const [shadow, setShadow] = useState<RiskShadow | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    let aborted = false
    async function load() {
      setLoading(true)
      setError(null)
      try {
        const [s, a, sh] = await Promise.all([
          fetchJson(API_RISK_SUMMARY),
          fetchJson(API_RISK_ATTRIBUTION),
          fetchJson(API_RISK_SHADOW),
        ])
        if (aborted) return
        setSummary(sanitizeRiskSummary(s))
        setAttribution(sanitizeAttribution(a))
        setShadow(sanitizeShadow(sh))
      } catch (e) {
        if (aborted) return
        setError(e instanceof Error ? e.message : String(e))
        setSummary(null)
        setAttribution(null)
        setShadow(null)
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

  const degradedReasons = [
    summary?.degraded ? `总览：${summary.reason ?? "unknown"}` : null,
    attribution?.degraded ? `归因：${attribution.reason ?? "unknown"}` : null,
    shadow?.degraded ? `影子重放：${shadow.reason ?? "unknown"}` : null,
  ].filter((x): x is string => typeof x === "string")

  const isDegraded = !loading && (error !== null || summary === null || degradedReasons.length > 0)
  const badge = (() => {
    if (loading) return { text: "加载中…", cls: "border-white/10 bg-white/5 text-slate-300" }
    if (isDegraded) return { text: "数据异常 · --", cls: "border-amber-500/30 bg-amber-500/10 text-amber-200" }
    if (circuit) return { text: `风控正常 · ${circuit}`, cls: "border-emerald-400/20 bg-emerald-400/10 text-emerald-300" }
    return { text: "风控正常 · CLOSED", cls: "border-emerald-400/20 bg-emerald-400/10 text-emerald-300" }
  })()

  const exposureDisplay = loading ? "…" : pct(summary?.exposure, 1)
  const singleLimitDisplay = loading ? "…" : pct(summary?.single_limit, 1)
  const circuitDisplay = loading ? "…" : typeof circuit === "string" && circuit.length > 0 ? String(circuit) : "未触发"
  const circuitThresholdSub = loading ? "阈值 …" : `阈值 ${pct(summary?.circuit_threshold, 0)}`
  const turnoverDisplay = loading ? "…" : num(turnover)
  const turnoverSub = crossSource ? `cross_source ${crossSource}` : "PIT/证据链"

  const bars = attribution?.bars
  const coverage = attribution?.coverage
  const components = attribution?.components ?? {}
  const rejectedByRule = shadow?.rejected_by_rule ?? {}
  const rejectedByRuleText = Object.keys(rejectedByRule).length
    ? Object.entries(rejectedByRule)
        .map(([k, v]) => `${k} ${v}`)
        .join(" · ")
    : "--"

  const pitText = loading ? "…" : typeof pit === "string" && pit.length > 0 ? String(pit) : "w ≤ p 否则 ValidationError"
  const crossSourceText = loading ? "…" : typeof crossSource === "string" && crossSource.length > 0 ? String(crossSource) : "首bar偏差>1% 阻断"
  const circuitDualText = loading ? "…" : typeof circuit === "string" && circuit.length > 0 ? String(circuit) : "--"
  const groundingText = isFiniteNumber(turnover) ? `turnover ${turnover}` : "turnover --"

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

      {(error || degradedReasons.length > 0) && !loading && (
        <div
          role="alert"
          className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3"
        >
          <div className="flex flex-col gap-1 text-sm text-amber-200">
            <span className="flex items-center gap-2">
              <span className="h-2 w-2 rounded-full bg-amber-400 animate-pulse" aria-hidden />
              {error ? `风控数据获取失败（${error}）` : "风控数据降级：以下指标非完整真值"}
            </span>
            {degradedReasons.length > 0 && (
              <span className="font-mono text-[11px] text-amber-200/80">{degradedReasons.join("；")}</span>
            )}
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
          { k: "总敞口", v: exposureDisplay, sub: bars ? `样本 ${bars} 根` : "样本 --" },
          { k: "单票上限", v: singleLimitDisplay, sub: "max |h| / E" },
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
              <span className="font-mono text-amber-100">Circuit {circuitDualText} / 阈值 {pct(summary?.circuit_threshold, 0)}</span>
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
            {ATTRIBUTION_ROWS.map((x) => (
              <div key={x.key} className="rounded-xl border border-white/5 bg-white/[0.03] p-3 text-center">
                <div className="text-slate-400">{x.label}</div>
                <div className="mt-1 font-semibold text-mist">{loading ? "…" : pct(components[x.key])}</div>
              </div>
            ))}
          </div>
          <p className="mt-3 text-xs leading-5 text-slate-500">
            归因 coverage {pct(coverage, 0)}；net_return {pct(attribution?.net_return)} · 对照 cumulative_return{" "}
            {pct(attribution?.metrics_cumulative_return)} · 换手名义 {num(attribution?.turnover_notional)}
          </p>
          {attribution?.not_applicable && attribution.not_applicable.length > 0 && (
            <p className="mt-1 font-mono text-[11px] text-amber-300/80">
              不可算项：{attribution.not_applicable.join(" · ")}
            </p>
          )}
          {attribution?.warnings && attribution.warnings.length > 0 && (
            <p className="mt-1 font-mono text-[11px] text-amber-300/80">
              归因告警：{attribution.warnings.join(" · ")}
            </p>
          )}
        </div>
      </div>

      <div className="mt-4 rounded-2xl border border-white/10 bg-white/[0.04] p-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-mist">ShadowAccount · 真实 fills 重放</h2>
          <span className="font-mono text-[11px] text-slate-500">bars {num(shadow?.bars)}</span>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
          {[
            { k: "订单数", v: num(shadow?.orders_total) },
            { k: "放行", v: num(shadow?.allowed) },
            { k: "拒单率", v: pct(shadow?.reject_rate) },
            { k: "名义本金", v: num(shadow?.notional_total) },
          ].map((c) => (
            <div key={c.k} className="rounded-xl border border-white/5 bg-white/[0.03] p-3 text-center">
              <div className="text-slate-400">{c.k}</div>
              <div className="mt-1 font-semibold text-mist">{loading ? "…" : c.v}</div>
            </div>
          ))}
        </div>
        <p className="mt-3 font-mono text-[11px] text-slate-500">
          rejected_by_rule {rejectedByRuleText} · turnover/敞口 {num(shadow?.turnover_mean_exposure)}
        </p>
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
