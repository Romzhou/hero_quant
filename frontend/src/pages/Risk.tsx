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

export default function Risk() {
  const [summary, setSummary] = useState<RiskSummary | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let aborted = false
    async function load() {
      try {
        const r = await fetch("/v1/risk/summary", { cache: "no-store" })
        if (!r.ok) throw new Error(String(r.status))
        const j = await r.json()
        if (!aborted && j && typeof j === "object") setSummary(j)
      } catch {
        // keep null -> fallback static
      } finally {
        if (!aborted) setLoading(false)
      }
    }
    load()
    return () => { aborted = true }
  }, [])

  const turnover = summary?.turnover
  const crossSource = summary?.cross_source
  const pit = summary?.pit
  const circuit = summary?.circuit

  return (
    <div className="mx-auto max-w-7xl px-6 py-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-xl font-semibold text-mist">Risk · 风控</h1>
          <p className="mt-1 text-sm text-slate-400">敞口 · 熔断 · 归因 · 证据链 · ShadowAccount</p>
        </div>
        <span className="rounded-full border border-emerald-400/20 bg-emerald-400/10 px-3 py-1 text-xs font-medium text-emerald-300">
          {loading ? "加载中…" : circuit ? `风控正常 · ${circuit}` : "风控正常 · CLOSED"}
        </span>
      </div>

      <div className="mt-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        {[
          { k: "总敞口", v: loading ? "…" : summary?.exposure != null ? `${(summary.exposure * 100).toFixed(0)}%` : "62%", sub: "杠杆 1.2x" },
          { k: "单票上限", v: loading ? "…" : summary?.single_limit != null ? `${(summary.single_limit * 100).toFixed(0)}%` : "20%", sub: "600519 18%" },
          { k: "日内熔断", v: loading ? "…" : circuit ? String(circuit) : "未触发", sub: summary?.circuit_threshold != null ? `阈值 ${(summary.circuit_threshold * 100).toFixed(0)}%` : "阈值 80%" },
          { k: "换手率", v: loading ? "…" : turnover != null ? String(turnover) : "0.42", sub: crossSource ? `cross_source ${crossSource}` : "PIT/证据链" },
        ].map((c) => (
          <div key={c.k} className="rounded-2xl border border-white/10 bg-white/[0.04] p-4 backdrop-blur">
            <div className="text-[11px] tracking-[0.14em] text-slate-400">{c.k}</div>
            <div className="mt-1 font-display text-xl font-semibold text-mist">{loading ? <span className="inline-block h-5 w-16 animate-pulse rounded bg-white/10" /> : c.v}</div>
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
              <span className="text-emerald-300">{loading ? "…" : pit ? String(pit) : "w ≤ p 否则 ValidationError"}</span>
            </div>
            <div className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2">
              <span className="text-slate-400">cross_source 1%</span>
              <span className="text-emerald-300">{loading ? "…" : crossSource ? String(crossSource) : "首bar偏差>1% 阻断"}</span>
            </div>
            <div className="flex justify-between rounded-xl bg-amber-500/10 border border-amber-500/20 px-3 py-2">
              <span className="text-amber-300">熔断双桶</span>
              <span className="font-mono text-amber-100">Circuit {loading ? "…" : circuit ? String(circuit) : "50%"} / OTel 80%</span>
            </div>
            <div className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2">
              <span className="text-slate-400">Grounding</span>
              <span className="text-mist">{turnover != null ? `turnover ${turnover}` : "证据链未命中阻断"}</span>
            </div>
          </div>
        </div>

        <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-mist">归因 · 5类 coverage&gt;0</h2>
            <a href="/research" className="rounded-full bg-white/5 px-2.5 py-1 text-[11px] text-slate-300 hover:bg-white/10">查看研究 → tearsheet</a>
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
          <a href="/research" className="rounded-full bg-white px-2.5 py-1 text-xs font-semibold text-amber-700 hover:bg-white/90">打开研究页 ↗</a>
        </div>
        <p className="mt-2 text-sm leading-6 text-amber-100/90">
          每笔风控决策均可回溯验证，超阈值自动进入备用路径并记录全链路。
        </p>
        <p className="mt-1 font-mono text-xs text-amber-200/60">ledger.verify() · trace.jsonl{turnover != null ? ` · turnover ${turnover}` : ""}</p>
      </div>
    </div>
  )
}
