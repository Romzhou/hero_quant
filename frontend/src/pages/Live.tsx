import { useEffect, useRef, useState } from "react"

type LiveEvent = { ts: string; offset: number; type: string; tool?: string; msg?: string; cost?: number }

export default function Live() {
  const [events, setEvents] = useState<LiveEvent[]>(() => [
    { ts: new Date().toISOString(), offset: 0, type: "trace", msg: "TraceWriter init · sidecar阈值50k" },
    { ts: new Date().toISOString(), offset: 1, type: "tool", tool: "get_market_data", msg: "600519.SH 天勤 · synthetic回退" },
    { ts: new Date().toISOString(), offset: 2, type: "tool", tool: "run_backtest", msg: "PIT校验通过 · positions.csv 已落盘" },
    { ts: new Date().toISOString(), offset: 3, type: "otel", msg: "OTel span: research_graph.execute 42ms" },
  ])
  const [offset, setOffset] = useState(4)
  const [paused, setPaused] = useState(false)
  const [cost, setCost] = useState(3.2)
  const costLimit = 5.0
  const ratio = Math.min(cost / costLimit, 1)
  const breakerState = ratio >= 1 ? "OPEN" : ratio >= 0.8 ? "HALF_OPEN" : "CLOSED"
  const listRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (paused) return
    let aborted = false
    let curOffset = offset
    const mock = setInterval(() => {
      if (aborted || paused) return
      if (Math.random() < 0.3) {
        const types = ["tool", "otel", "circuit", "trace"] as const
        const t = types[Math.floor(Math.random() * types.length)]
        setEvents((prev) => [
          ...prev.slice(-199),
          {
            ts: new Date().toISOString(),
            offset: curOffset++,
            type: t,
            tool: t === "tool" ? ["get_bars", "run_backtest", "calc_rsi"][Math.floor(Math.random() * 3)] : undefined,
            msg: t === "otel" ? `cost +$0.002 · span langgraph.invoke` : t === "circuit" ? `Circuit ${breakerState} · 阈值50%` : `heartbeat · events.jsonl offset ${curOffset}`,
            cost: t === "otel" ? +(cost + 0.002).toFixed(3) : undefined,
          },
        ])
        setOffset(curOffset)
        if (t === "otel") setCost((c) => Math.min(c + 0.002, costLimit + 0.5))
      }
    }, 1200)
    return () => {
      aborted = true
      clearInterval(mock)
    }
  }, [paused, offset, cost, breakerState])

  useEffect(() => {
    const el = listRef.current as unknown as { scrollTo?: (o: unknown) => void; scrollTop?: number; scrollHeight?: number } | null
    if (el?.scrollTo) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" })
    else if (el && typeof el.scrollTop === "number") el.scrollTop = el.scrollHeight ?? 0
  }, [events])

  return (
    <div className="mx-auto max-w-7xl px-6 py-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-xl font-semibold text-mist">Live 监控 · 实盘</h1>
          <p className="mt-1 text-sm text-slate-400">events.jsonl offset 实时 · OTel 三档遥测 · 成本熔断</p>
        </div>
        <div className="flex items-center gap-2">
          <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 font-mono text-xs text-slate-300">offset: {offset}</span>
          <span
            className={
              "rounded-full border px-3 py-1 text-xs font-semibold " +
              (breakerState === "OPEN"
                ? "border-red-400/30 bg-red-400/15 text-red-300"
                : breakerState === "HALF_OPEN"
                  ? "border-amber-400/30 bg-amber-400/15 text-amber-300"
                  : "border-emerald-400/20 bg-emerald-400/10 text-emerald-300")
            }
          >
            {breakerState}
          </span>
          <button
            onClick={() => setPaused((p) => !p)}
            className={"rounded-xl px-3.5 py-1.5 text-xs font-semibold transition " + (paused ? "bg-white text-ink-900" : "bg-white/10 text-mist hover:bg-white/15")}
          >
            {paused ? "▶ 恢复" : "⏸ 暂停"}
          </button>
        </div>
      </div>

      <div className="mt-6 rounded-2xl border border-white/10 bg-ink-800/60 p-4 backdrop-blur">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-mist">OTel cost 熔断条</h2>
          <span className="font-mono text-xs text-slate-400">
            daily {cost.toFixed(3)} / {costLimit.toFixed(1)} USD · {breakerState}
          </span>
        </div>
        <div className="mt-3 h-3 w-full overflow-hidden rounded-full bg-ink-900 border border-white/5">
          <div
            className={
              "h-full rounded-full transition-all duration-700 " +
              (ratio >= 1
                ? "bg-gradient-to-r from-red-500 to-red-600"
                : ratio >= 0.8
                  ? "bg-gradient-to-r from-amber-400 to-orange-500"
                  : "bg-gradient-to-r from-emerald-400 to-teal-500")
            }
            style={{ width: `${Math.min(ratio * 100, 100)}%` }}
          />
        </div>
        <div className="mt-2 flex justify-between text-[11px] text-slate-500">
          <span>0</span>
          <span className={ratio >= 0.8 ? "text-amber-300 font-semibold" : ""}>阈值 80% 预警</span>
          <span className={ratio >= 1 ? "text-red-300 font-semibold" : ""}>熔断 {costLimit.toFixed(1)}</span>
        </div>
      </div>

      <div className="mt-4 rounded-2xl border border-white/10 bg-ink-800/60 backdrop-blur overflow-hidden flex flex-col">
        <div className="flex items-center justify-between border-b border-white/5 px-4 py-3">
          <h3 className="text-sm font-semibold text-mist">events.jsonl · Live 实时</h3>
          <span className="rounded-full bg-white/5 px-2.5 py-1 font-mono text-[11px] text-slate-400">offset 增量</span>
        </div>
        <div ref={listRef} className="h-[420px] overflow-auto bg-ink-900/40 font-mono text-xs leading-5">
          <div className="sticky top-0 bg-ink-900/80 backdrop-blur border-b border-white/5 px-3 py-1.5 flex gap-4 text-[11px] text-slate-500">
            <span className="w-16">offset</span>
            <span className="w-20">type</span>
            <span>message</span>
          </div>
          {events.map((e) => (
            <div key={e.offset} className="flex gap-4 px-3 py-1.5 border-b border-white/[0.03] hover:bg-white/[0.03] transition">
              <span className="w-16 shrink-0 text-slate-500">{e.offset}</span>
              <span
                className={
                  "w-20 shrink-0 rounded-full px-1.5 py-0.5 text-center text-[10px] font-semibold " +
                  (e.type === "tool"
                    ? "bg-amber-500/15 text-amber-300 border border-amber-500/20"
                    : e.type === "otel"
                      ? "bg-sky-500/15 text-sky-300 border border-sky-500/20"
                      : e.type === "circuit"
                        ? "bg-red-500/15 text-red-300 border border-red-500/20"
                        : "bg-white/5 text-slate-300 border border-white/10")
                }
              >
                {e.type}
                {e.tool ? `:${e.tool}` : ""}
              </span>
              <span className="flex-1 truncate text-slate-300">{e.msg}</span>
              <span className="hidden md:inline shrink-0 text-[10px] text-slate-500">{new Date(e.ts).toLocaleTimeString()}</span>
            </div>
          ))}
        </div>
        <div className="border-t border-white/5 bg-white/[0.02] px-3 py-2 flex items-center gap-2">
          <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
          <span className="text-xs text-slate-400">live · {paused ? "已暂停" : "实时拉取"} · events.jsonl offset={offset}</span>
        </div>
      </div>
    </div>
  )
}
