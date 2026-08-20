import { useEffect, useRef, useState } from "react"

type LiveEvent = { ts: string; offset: number; type: string; tool?: string; msg?: string; cost?: number }

export default function Monitor() {
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
  const esRef = useRef<EventSource | null>(null)

  // 实时 SSE：events.jsonl offset 增量拉取
  useEffect(() => {
    if (paused) return
    let aborted = false
    let curOffset = offset

    async function streamFetch() {
      // 优先尝试 SSE 接口
      const candidates = [
        `/v1/trace/events?offset=${curOffset}`,
        `/v1/events?offset=${curOffset}`,
        `/v1/query/stream?offset=${curOffset}`,
      ]
      for (const url of candidates) {
        try {
          const resp = await fetch(url, { headers: { Accept: "text/event-stream" } })
          if (!resp.ok || !resp.body) continue
          const reader = resp.body.getReader()
          const decoder = new TextDecoder()
          let buf = ""
          while (!aborted && !paused) {
            const { done, value } = await reader.read()
            if (done) break
            buf += decoder.decode(value, { stream: true })
            const parts = buf.split("\n\n")
            buf = parts.pop() || ""
            for (const p of parts) {
              const line = p.split("\n").find(l => l.startsWith("data:"))
              if (!line) continue
              const raw = line.replace(/^data:\s*/, "")
              if (raw === "[DONE]") break
              try {
                const j = JSON.parse(raw)
                const ev: LiveEvent = {
                  ts: j.ts || new Date().toISOString(),
                  offset: j.offset ?? curOffset,
                  type: j.type || "event",
                  tool: j.tool,
                  msg: j.msg || j.delta || raw.slice(0, 120),
                  cost: j.cost
                }
                setEvents(prev => [...prev.slice(-199), ev])
                if (typeof j.cost === "number") setCost(j.cost)
                curOffset = (j.offset ?? curOffset) + 1
                setOffset(curOffset)
              } catch {
                setEvents(prev => [...prev.slice(-199), { ts: new Date().toISOString(), offset: curOffset++, type: "raw", msg: raw.slice(0, 160) }])
              }
            }
          }
          return // 成功则不再试下一候选
        } catch {
          // try next
        }
      }
      // 回退：EventSource
      try {
        const es = new EventSource(`/v1/trace/events?offset=${curOffset}`)
        esRef.current = es
        es.onmessage = e => {
          try {
            const j = JSON.parse(e.data)
            setEvents(prev => [...prev.slice(-199), { ts: j.ts || new Date().toISOString(), offset: j.offset ?? curOffset, type: j.type || "event", msg: j.msg || e.data.slice(0, 120) }])
            curOffset++
            setOffset(curOffset)
          } catch {
            setEvents(prev => [...prev.slice(-199), { ts: new Date().toISOString(), offset: curOffset++, type: "sse", msg: e.data.slice(0, 140) }])
          }
        }
        es.onerror = () => { es.close() }
      } catch {}
    }

    streamFetch()
    // Mock 增量（当后端不可达时保持 live 感）
    const mock = setInterval(() => {
      if (aborted || paused) return
      // 30% 概率追加 mock
      if (Math.random() < 0.3) {
        const types = ["tool", "otel", "circuit", "trace"] as const
        const t = types[Math.floor(Math.random() * types.length)]
        setEvents(prev => [...prev.slice(-199), {
          ts: new Date().toISOString(),
          offset: curOffset++,
          type: t,
          tool: t === "tool" ? ["get_bars","run_backtest","calc_rsi"][Math.floor(Math.random()*3)] : undefined,
          msg: t === "otel" ? `cost +$0.002 · span langgraph.invoke` : t === "circuit" ? `Circuit ${breakerState} · 阈值50%` : `heartbeat · events.jsonl offset ${curOffset}`,
          cost: t === "otel" ? +(cost + 0.002).toFixed(3) : undefined
        }])
        setOffset(curOffset)
        if (t === "otel") setCost(c => Math.min(c + 0.002, costLimit + 0.5))
      }
    }, 1200)

    return () => { aborted = true; clearInterval(mock); esRef.current?.close() }
  }, [paused, offset, cost, breakerState])

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" })
  }, [events])

  return (
    <div className="mx-auto max-w-7xl px-6 py-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-xl font-semibold text-mist">Live 监控 · 运行态</h1>
          <p className="mt-1 text-sm text-slate-400">events.jsonl offset 实时 SSE · OTel 三档遥测 · 成本熔断</p>
        </div>
        <div className="flex items-center gap-2">
          <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 font-mono text-xs text-slate-300">offset: {offset}</span>
          <span className={"rounded-full border px-3 py-1 text-xs font-semibold " + (breakerState==="OPEN" ? "border-red-400/30 bg-red-400/15 text-red-300" : breakerState==="HALF_OPEN" ? "border-amber-400/30 bg-amber-400/15 text-amber-300" : "border-emerald-400/20 bg-emerald-400/10 text-emerald-300")}>{breakerState}</span>
          <button onClick={() => setPaused(p=>!p)} className={"rounded-xl px-3.5 py-1.5 text-xs font-semibold transition " + (paused ? "bg-white text-ink-900" : "bg-white/10 text-mist hover:bg-white/15")}>{paused ? "▶ 恢复" : "⏸ 暂停"}</button>
          <button onClick={() => setEvents([])} className="rounded-xl border border-white/10 bg-white/5 px-3 py-1.5 text-xs text-slate-300 hover:bg-white/10">清空</button>
        </div>
      </div>

      {/* OTel cost 熔断条 */}
      <div className="mt-6 rounded-2xl border border-white/10 bg-ink-800/60 p-4 backdrop-blur">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-mist">OTel cost 熔断条</h2>
          <span className="font-mono text-xs text-slate-400">daily {cost.toFixed(3)} / {costLimit.toFixed(1)} USD · {breakerState}</span>
        </div>
        <div className="mt-3 h-3 w-full overflow-hidden rounded-full bg-ink-900 border border-white/5">
          <div
            className={"h-full rounded-full transition-all duration-700 " + (ratio >= 1 ? "bg-gradient-to-r from-red-500 to-red-600" : ratio >= 0.8 ? "bg-gradient-to-r from-amber-400 to-orange-500" : "bg-gradient-to-r from-emerald-400 to-teal-500")}
            style={{ width: `${Math.min(ratio*100,100)}%` }}
          />
        </div>
        <div className="mt-2 flex justify-between text-[11px] text-slate-500">
          <span>0</span>
          <span className={ratio>=0.8 ? "text-amber-300 font-semibold" : ""}>阈值 80% 预警</span>
          <span className={ratio>=1 ? "text-red-300 font-semibold" : ""}>熔断 {costLimit.toFixed(1)}</span>
        </div>
        <div className="mt-3 grid grid-cols-3 gap-3 text-xs">
          <div className="rounded-xl bg-white/[0.04] border border-white/5 p-3">
            <div className="text-slate-400">OTel 模式</div>
            <div className="mt-1 font-semibold text-mist">telemetry/otel.py · disabled|basic|full</div>
          </div>
          <div className="rounded-xl bg-white/[0.04] border border-white/5 p-3">
            <div className="text-slate-400">Collector</div>
            <div className="mt-1 font-mono text-mist">Langfuse / Honeycomb 占位</div>
          </div>
          <div className="rounded-xl bg-amber-500/10 border border-amber-500/20 p-3">
            <div className="text-amber-300">BudgetBreaker</div>
            <div className="mt-1 text-amber-100/80">滑动窗口熔断 · 超限走 fallback</div>
          </div>
        </div>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-5">
        {/* events.jsonl 流 */}
        <div className="lg:col-span-3 rounded-2xl border border-white/10 bg-ink-800/60 backdrop-blur overflow-hidden flex flex-col">
          <div className="flex items-center justify-between border-b border-white/5 px-4 py-3">
            <h3 className="text-sm font-semibold text-mist">events.jsonl · 实时 SSE</h3>
            <span className="rounded-full bg-white/5 px-2.5 py-1 font-mono text-[11px] text-slate-400">tail -f · offset 增量</span>
          </div>
          <div ref={listRef} className="h-[420px] overflow-auto bg-ink-900/40 font-mono text-xs leading-5">
            <div className="sticky top-0 bg-ink-900/80 backdrop-blur border-b border-white/5 px-3 py-1.5 flex gap-4 text-[11px] text-slate-500">
              <span className="w-16">offset</span><span className="w-20">type</span><span>message</span>
            </div>
            {events.map(e => (
              <div key={e.offset} className="flex gap-4 px-3 py-1.5 border-b border-white/[0.03] hover:bg-white/[0.03] transition">
                <span className="w-16 shrink-0 text-slate-500">{e.offset}</span>
                <span className={
                  "w-20 shrink-0 rounded-full px-1.5 py-0.5 text-center text-[10px] font-semibold " +
                  (e.type==="tool" ? "bg-amber-500/15 text-amber-300 border border-amber-500/20" : e.type==="otel" ? "bg-sky-500/15 text-sky-300 border border-sky-500/20" : e.type==="circuit" ? "bg-red-500/15 text-red-300 border border-red-500/20" : "bg-white/5 text-slate-300 border border-white/10")
                }>{e.type}{e.tool ? `:${e.tool}` : ""}</span>
                <span className="flex-1 truncate text-slate-300">{e.msg}</span>
                <span className="hidden md:inline shrink-0 text-[10px] text-slate-500">{new Date(e.ts).toLocaleTimeString()}</span>
              </div>
            ))}
            {!events.length && <div className="p-8 text-center text-slate-500">暂无事件 · 等待 SSE 推送</div>}
          </div>
          <div className="border-t border-white/5 bg-white/[0.02] px-3 py-2 flex items-center gap-2">
            <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
            <span className="text-xs text-slate-400">live · {paused ? "已暂停" : "实时拉取"} · events.jsonl offset={offset}</span>
            <span className="ml-auto text-[11px] text-slate-500">trace.jsonl sidecar阈值 50k · 硬阈值 500 预览</span>
          </div>
        </div>

        {/* 右侧：链路与熔断详情 */}
        <div className="lg:col-span-2 flex flex-col gap-4">
          <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
            <h3 className="text-sm font-semibold text-mist">心跳四层 + 熔断双桶</h3>
            <div className="mt-3 space-y-2 text-xs">
              <div className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2">
                <span className="text-slate-400">HeartbeatTimer</span><span className="text-emerald-300">daemon · 0.5s 看门狗</span>
              </div>
              <div className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2">
                <span className="text-slate-400">CircuitBreaker</span><span className="font-mono text-mist">50% / 30s open</span>
              </div>
              <div className="flex justify-between rounded-xl bg-amber-500/10 border border-amber-500/20 px-3 py-2">
                <span className="text-amber-300">OTel cost 熔断条</span><span className="font-mono text-amber-100">{ratio>=0.8 ? "将触发 fallback" : "正常"}</span>
              </div>
            </div>
            <div className="mt-3 rounded-xl border border-white/5 bg-ink-900/50 p-3 text-xs leading-5 text-slate-400">
              熔断状态由 <code className="rounded bg-white/10 px-1 text-mist">BudgetBreaker</code> 与 <code className="rounded bg-white/10 px-1 text-mist">CircuitBreaker</code> 共同决定；达阈值时图执行走 <span className="text-amber-300">compensate</span> 分支。
            </div>
          </div>

          <div className="rounded-2xl border border-white/10 bg-ink-800/60 p-4 backdrop-blur">
            <h3 className="text-sm font-semibold text-mist">调用链路</h3>
            <div className="mt-3 relative pl-6">
              <div className="absolute left-2 top-2 bottom-2 w-px bg-gradient-to-b from-amber-400/50 to-white/10" />
              {[
                { step: "plan", desc: "Subagents 研究团队分派", state: "done" },
                { step: "execute", desc: "工具并发 · market/backtest/quantlib", state: "running" },
                { step: "verify", desc: "PIT + grounding + ledger", state: "pending" },
                { step: "report", desc: "tearsheet.html 生成", state: "pending" },
              ].map(s => (
                <div key={s.step} className="relative mb-3 flex items-center gap-3">
                  <div className={"absolute -left-6 h-2.5 w-2.5 rounded-full border " + (s.state==="done" ? "bg-emerald-400 border-emerald-300" : s.state==="running" ? "bg-amber-400 border-amber-300 animate-pulse" : "bg-white/10 border-white/20")} />
                  <div className="flex-1 rounded-xl border border-white/5 bg-white/[0.03] px-3 py-2">
                    <div className="font-mono text-xs font-semibold text-mist">{s.step}</div>
                    <div className="text-xs text-slate-400">{s.desc}</div>
                  </div>
                  <span className={"text-[10px] rounded-full px-2 py-1 " + (s.state==="done" ? "bg-emerald-400/10 text-emerald-300" : s.state==="running" ? "bg-amber-400/10 text-amber-300" : "bg-white/5 text-slate-500")}>{s.state}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="rounded-2xl border border-emerald-400/15 bg-emerald-400/10 p-4">
            <div className="text-xs font-semibold tracking-widest text-emerald-300">可观测</div>
            <p className="mt-2 text-sm leading-6 text-emerald-100/90">前端通过 <span className="font-mono">offset</span> 增量拉取 <span className="font-mono">events.jsonl</span>，与后端 <span className="font-mono">TraceWriter(HardLink)</span> 同源；OTel 数据经 Collector 汇至 Langfuse/Honeycomb。</p>
          </div>
        </div>
      </div>
    </div>
  )
}
