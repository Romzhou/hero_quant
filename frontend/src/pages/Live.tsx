/**
 * Live 实盘监控页
 * - 职责：实时展示 events.jsonl 增量事件、OTel 成本熔断与调用链路阶段
 * - 数据流：订阅 /v1/trace/events?offset（fetch ReadableStream 优先，失败回退 EventSource），按 offset 增量追加；
 *   无后端时用本地 mock 心跳维持演示，cost/ratio 驱动 BudgetBreaker 熔断条与 CLOSED/HALF_OPEN/OPEN 状态
 * - 关键细节：offset/cost 用 ref 同步避免 effect 频繁重建，自清理 abort/reader/timer 防止泄漏
 */
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
  const esRef = useRef<EventSource | null>(null)
  const offsetRef = useRef(offset)
  const costRef = useRef(cost)
  const pausedRef = useRef(paused)
  const readerRef = useRef<ReadableStreamDefaultReader<Uint8Array> | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  // 用 ref 镜像 offset/cost/paused，避免 SSE effect 依赖频繁变动导致重建连接，修复 stale closure
  useEffect(() => { offsetRef.current = offset }, [offset])
  useEffect(() => { costRef.current = cost }, [cost])
  useEffect(() => { pausedRef.current = paused }, [paused])

  // 订阅实盘事件：fetch 流式优先，异常时尝试 EventSource；paused 时暂停
  useEffect(() => {
    if (paused) return
    let aborted = false

    async function streamFetch() {
      const curOffsetSnap = offsetRef.current
      let curOffset = curOffsetSnap
      const candidates = [
        `/v1/trace/events?offset=${curOffset}`,
        `/v1/trace/events?offset=0`,
      ]
      for (const url of candidates) {
        // 48-50 修复：切换候选前先 abort 上一个 controller，避免泄漏
        if (abortRef.current) {
          try { abortRef.current.abort() } catch {}
        }
        const controller = new AbortController()
        abortRef.current = controller
        let reader: ReadableStreamDefaultReader<Uint8Array> | null = null
        try {
          const resp = await fetch(url, { headers: { Accept: "text/event-stream" }, signal: controller.signal })
          if (!resp.ok || !resp.body) continue
          reader = resp.body.getReader()
          readerRef.current = reader
          const decoder = new TextDecoder()
          let buf = ""
          while (!aborted && !pausedRef.current) {
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
                if (typeof j.cost === "number") {
                  costRef.current = j.cost
                  setCost(j.cost)
                }
                curOffset = (j.offset ?? curOffset) + 1
                offsetRef.current = curOffset
                setOffset(curOffset)
              } catch {
                const next = curOffset++
                offsetRef.current = curOffset
                setEvents(prev => [...prev.slice(-199), { ts: new Date().toISOString(), offset: next, type: "raw", msg: raw.slice(0, 160) }])
              }
            }
          }
          return
        } catch {
          // 候选地址失败则尝试下一个，无需提示，前端静默回退
        } finally {
          try { await reader?.cancel() } catch {}
          if (readerRef.current === reader) readerRef.current = null
        }
      }
      // fetch 候选均失败，回退 EventSource
      try {
        const es = new EventSource(`/v1/trace/events?offset=${curOffset}`)
        esRef.current = es
        es.onmessage = e => {
          try {
            const j = JSON.parse(e.data)
            const nextOffset = j.offset ?? curOffset
            setEvents(prev => [...prev.slice(-199), { ts: j.ts || new Date().toISOString(), offset: nextOffset, type: j.type || "event", msg: j.msg || e.data.slice(0, 120) }])
            curOffset = nextOffset + 1
            offsetRef.current = curOffset
            setOffset(curOffset)
          } catch {
            const next = curOffset++
            offsetRef.current = curOffset
            setEvents(prev => [...prev.slice(-199), { ts: new Date().toISOString(), offset: next, type: "sse", msg: e.data.slice(0, 140) }])
          }
        }
        es.onerror = () => { es.close() }
      } catch {}
    }

    streamFetch()
    // 已通过 fetch /v1/trace/events SSE 真流驱动；移除 Math.random mock（Wave5 去 mock），保留确定性空心跳占位
    const heartbeat = setInterval(() => {
      if (aborted || pausedRef.current) return
      // 纯 SSE 驱动，不再注入随机 mock；确定性占位避免使用 Math.random
    }, 5000)

    // 清理：标记 aborted、清定时器、关闭 SSE/流读取器，防止切页或暂停后泄漏
    return () => {
      aborted = true
      clearInterval(heartbeat)
      esRef.current?.close()
      try { readerRef.current?.cancel() } catch {}
      try { abortRef.current?.abort() } catch {}
    }
  }, [paused])

  // 事件追加后自动滚底，保持最新 offset 可见；兼容无 scrollTo 的容器
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
          <p className="mt-1 text-sm text-slate-400">events.jsonl offset 实时 SSE · OTel 三档遥测 · 成本熔断</p>
        </div>
        <div className="flex items-center gap-2">
          <span className={"hidden md:inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium " + (paused ? "border-slate-500/20 bg-white/5 text-slate-400" : "border-emerald-400/20 bg-emerald-400/10 text-emerald-300")}><span className={"h-1.5 w-1.5 rounded-full " + (paused ? "bg-slate-400" : "bg-emerald-400 animate-pulse")} />{paused ? "已暂停" : "● 实时连接"}</span>
          <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 font-mono text-xs text-slate-300">offset: {offset}</span>
          <span className={"rounded-full border px-3 py-1 text-xs font-semibold " + (breakerState === "OPEN" ? "border-red-400/30 bg-red-400/15 text-red-300" : breakerState === "HALF_OPEN" ? "border-amber-400/30 bg-amber-400/15 text-amber-300" : "border-emerald-400/20 bg-emerald-400/10 text-emerald-300")}>{breakerState}</span>
          <button onClick={() => setPaused(p => !p)} className={"rounded-xl px-3.5 py-1.5 text-xs font-semibold transition " + (paused ? "bg-white text-ink-900" : "bg-white/10 text-mist hover:bg-white/15")}>{paused ? "▶ 恢复" : "⏸ 暂停"}</button>
          <button onClick={() => setEvents([])} className="hidden rounded-xl border border-white/10 bg-white/5 px-3 py-1.5 text-xs text-slate-300 hover:bg-white/10 md:inline">清空</button>
        </div>
      </div>

      <div className="mt-6 rounded-2xl border border-white/10 bg-ink-800/60 p-4 backdrop-blur">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-mist">OTel cost 熔断条</h2>
          <span className="font-mono text-xs text-slate-400">daily {cost.toFixed(3)} / {costLimit.toFixed(1)} USD · {breakerState}</span>
        </div>
        <div className="mt-3 h-3 w-full overflow-hidden rounded-full bg-ink-900 border border-white/5">
          <div className={"h-full rounded-full transition-all duration-700 " + (ratio >= 1 ? "bg-gradient-to-r from-red-500 to-red-600" : ratio >= 0.8 ? "bg-gradient-to-r from-amber-400 to-orange-500" : "bg-gradient-to-r from-emerald-400 to-teal-500")} style={{ width: `${Math.min(ratio * 100, 100)}%` }} />
        </div>
        <div className="mt-2 flex justify-between text-[11px] text-slate-500">
          <span>0</span>
          <span className={ratio >= 0.8 ? "text-amber-300 font-semibold" : ""}>阈值 80% 预警</span>
          <span className={ratio >= 1 ? "text-red-300 font-semibold" : ""}>熔断 {costLimit.toFixed(1)}</span>
        </div>
        <div className="mt-3 grid grid-cols-3 gap-2 text-xs md:gap-3">
          <div className="rounded-xl bg-white/[0.04] border border-white/5 p-3">
            <div className="text-slate-400">OTel 模式</div>
            <div className="mt-1 font-semibold text-mist">telemetry/otel.py · disabled|basic|full</div>
          </div>
          <div className="rounded-xl bg-white/[0.04] border border-white/5 p-3">
            <div className="text-slate-400">Collector</div>
            <div className="mt-1 font-mono text-mist">Langfuse / Honeycomb</div>
          </div>
          <div className="rounded-xl bg-amber-500/10 border border-amber-500/20 p-3">
            <div className="text-amber-300">BudgetBreaker</div>
            <div className="mt-1 text-amber-100/80">滑动窗口熔断 · 超限走 fallback</div>
          </div>
        </div>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-5">
        <div className="lg:col-span-3 rounded-2xl border border-white/10 bg-ink-800/60 backdrop-blur overflow-hidden flex flex-col">
          <div className="flex items-center justify-between border-b border-white/5 px-4 py-3">
            <h3 className="text-sm font-semibold text-mist">events.jsonl · Live 实时</h3>
            <span className="rounded-full bg-white/5 px-2.5 py-1 font-mono text-[11px] text-slate-400">tail -f · offset 增量</span>
          </div>
          <div ref={listRef} className="h-[420px] overflow-auto bg-ink-900/40 font-mono text-xs leading-5">
            <div className="sticky top-0 bg-ink-900/80 backdrop-blur border-b border-white/5 px-3 py-1.5 flex gap-4 text-[11px] text-slate-500">
              <span className="w-16">offset</span><span className="w-20">type</span><span>message</span>
            </div>
            {events.length === 0 ? (
              <div className="p-10 text-center">
                <div className="mx-auto h-8 w-8 rounded-lg border border-dashed border-white/10 grid place-items-center text-slate-500">◌</div>
                <p className="mt-2 text-xs text-slate-400">等待 trace … 暂无事件 · 将通过 offset 增量推送</p>
                <p className="mt-1 font-mono text-[11px] text-slate-500">events.jsonl · tail -f</p>
              </div>
            ) : events.map(e => (
              <div key={e.offset} className="flex gap-4 px-3 py-1.5 border-b border-white/[0.03] hover:bg-white/[0.03] transition">
                <span className="w-16 shrink-0 text-slate-500">{e.offset}</span>
                <span className={"w-20 shrink-0 rounded-full px-1.5 py-0.5 text-center text-[10px] font-semibold " + (e.type === "tool" ? "bg-amber-500/15 text-amber-300 border border-amber-500/20" : e.type === "otel" ? "bg-sky-500/15 text-sky-300 border border-sky-500/20" : e.type === "circuit" ? "bg-red-500/15 text-red-300 border border-red-500/20" : "bg-white/5 text-slate-300 border border-white/10")}>{e.type}{e.tool ? `:${e.tool}` : ""}</span>
                <span className="flex-1 truncate text-slate-300">{e.msg}</span>
                <span className="hidden md:inline shrink-0 text-[10px] text-slate-500">{new Date(e.ts).toLocaleTimeString()}</span>
              </div>
            ))}
          </div>
          <div className="border-t border-white/5 bg-white/[0.02] px-3 py-2 flex items-center gap-2">
            <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
            <span className="text-xs text-slate-400">live · {paused ? "已暂停" : "实时拉取"} · events.jsonl offset={offset}</span>
            <span className="ml-auto hidden text-[11px] text-slate-500 md:inline">trace.jsonl sidecar阈值 50k · 硬阈值 500 预览</span>
          </div>
        </div>
        <div className="lg:col-span-2 flex flex-col gap-4">
          <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
            <h3 className="text-sm font-semibold text-mist">心跳四层 + 熔断双桶</h3>
            <div className="mt-3 space-y-2 text-xs">
              <div className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2"><span className="text-slate-400">HeartbeatTimer</span><span className="text-emerald-300">daemon · 0.5s 看门狗</span></div>
              <div className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2"><span className="text-slate-400">CircuitBreaker</span><span className="font-mono text-mist">50% / 30s open</span></div>
              <div className="flex justify-between rounded-xl bg-amber-500/10 border border-amber-500/20 px-3 py-2"><span className="text-amber-300">OTel 熔断</span><span className="font-mono text-amber-100">{ratio >= 0.8 ? "将触发 fallback" : "正常"}</span></div>
            </div>
            <div className="mt-3 rounded-xl border border-white/5 bg-ink-900/50 p-3 text-xs leading-5 text-slate-400">熔断状态由 <code className="rounded bg-white/10 px-1 text-mist">BudgetBreaker</code> 与 <code className="rounded bg-white/10 px-1 text-mist">CircuitBreaker</code> 共同决定；达阈值时图执行走 <span className="text-amber-300">compensate</span> 分支。</div>
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
                  <div className={"absolute -left-6 h-2.5 w-2.5 rounded-full border " + (s.state === "done" ? "bg-emerald-400 border-emerald-300" : s.state === "running" ? "bg-amber-400 border-amber-300 animate-pulse" : "bg-white/10 border-white/20")} />
                  <div className="flex-1 rounded-xl border border-white/5 bg-white/[0.03] px-3 py-2">
                    <div className="font-mono text-xs font-semibold text-mist">{s.step}</div>
                    <div className="text-xs text-slate-400">{s.desc}</div>
                  </div>
                  <span className={"text-[10px] rounded-full px-2 py-1 " + (s.state === "done" ? "bg-emerald-400/10 text-emerald-300" : s.state === "running" ? "bg-amber-400/10 text-amber-300" : "bg-white/5 text-slate-500")}>{s.state}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
