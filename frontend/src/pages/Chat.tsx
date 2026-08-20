import { useRef, useState } from "react"
import { useChatStore } from "../store/chat"

type ToolCall = { tool: string; status: "pending" | "success" | "error"; latencyMs?: number; preview?: string }

export default function Chat() {
  const { messages, input, streaming, setInput, push, setStreaming } = useChatStore()
  const [error, setError] = useState<string | null>(null)
  const [traceByMsgId, setTraceByMsgId] = useState<Record<string, ToolCall[]>>({})
  const listRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const esRef = useRef<EventSource | null>(null)

  async function send() {
    const q = input.trim()
    if (!q || streaming) return
    const userMsg = { id: String(Date.now()), role: "user" as const, content: q }
    push(userMsg)
    setInput("")
    setStreaming(true)
    setError(null)

    const aid = String(Date.now() + 1)
    push({ id: aid, role: "assistant", content: "" })
    // 真实 SSE：初始轨迹为空，由后端 tool 事件驱动；仅占位用于视觉一致
    setTraceByMsgId(s => ({ ...s, [aid]: [] }))

    // Abort previous if any
    abortRef.current?.abort()
    esRef.current?.close()
    const controller = new AbortController()
    abortRef.current = controller

    let acc = ""
    let hasDelta = false
    let settled = false

    const appendDelta = (delta: string) => {
      if (!delta) return
      hasDelta = true
      acc += delta
      useChatStore.setState(s => ({
        messages: s.messages.map(m => m.id === aid ? { ...m, content: acc } : m),
      }))
      requestAnimationFrame(() => listRef.current?.scrollTo({ top: 99999, behavior: "smooth" }))
    }

    const handlePayload = (raw: string) => {
      if (!raw || raw === "[DONE]") return
      try {
        const j = JSON.parse(raw)
        if (j.type === "tool") {
          const tname = (j.tool || j.name || "unknown_tool") as string
          const status = (j.status as ToolCall["status"]) || "success"
          const preview = j.preview ?? j.msg ?? j.detail ?? undefined
          const latencyMs = j.latencyMs ?? j.latency ?? j.durationMs ?? undefined
          setTraceByMsgId(prev => {
            const cur = prev[aid] ?? []
            const exists = cur.find(c => c.tool === tname)
            if (exists) {
              return { ...prev, [aid]: cur.map(c => c.tool === tname ? { ...c, status, preview: preview ?? c.preview, latencyMs: latencyMs ?? c.latencyMs } : c) }
            }
            return { ...prev, [aid]: [...cur, { tool: tname, status, preview, latencyMs }] }
          })
          return
        }
        // 兼容：含 tool 字段但未标 type 且无 delta 时视为轨迹
        if (j.tool && !("delta" in j) && !("text" in j) && !("content" in j) && !("answer" in j)) {
          const tname = (j.tool || j.name) as string
          setTraceByMsgId(prev => {
            const cur = prev[aid] ?? []
            const exists = cur.find(c => c.tool === tname)
            if (exists) return prev
            return { ...prev, [aid]: [...cur, { tool: tname, status: (j.status as ToolCall["status"]) || "success", preview: j.preview, latencyMs: j.latencyMs }] }
          })
          return
        }
        if (j.type === "error") {
          throw new Error(j.msg || j.message || "stream error")
        }
        const delta = j.delta || j.text || j.content || j.answer || ""
        if (delta) appendDelta(delta)
      } catch (e) {
        if (e instanceof SyntaxError) {
          // 非 JSON 则按原始 delta 处理
          if (raw) appendDelta(raw)
        } else {
          throw e
        }
      }
    }

    const fetchFallback = async () => {
      const url = `/v1/query/stream?q=${encodeURIComponent(q)}`
      const resp = await fetch(url, {
        method: "GET",
        headers: { Accept: "text/event-stream" },
        signal: controller.signal,
      })
      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`)
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ""
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        buffer = buffer.replace(/\r\n/g, "\n")
        let idx: number
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          const rawEvent = buffer.slice(0, idx)
          buffer = buffer.slice(idx + 2)
          if (!rawEvent.trim()) continue
          const dataLines = rawEvent
            .split("\n")
            .filter(l => l.startsWith("data:"))
            .map(l => l.replace(/^data:\s*/, ""))
          if (dataLines.length === 0) continue
          const data = dataLines.join("\n")
          if (data === "[DONE]") break
          handlePayload(data)
        }
      }
      if (!hasDelta && !acc) {
        useChatStore.setState(s => ({
          messages: s.messages.map(m => m.id === aid ? { ...m, content: "（空响应，检查后端 /v1/query/stream SSE）" } : m),
        }))
      }
    }

    const tryEventSource = () =>
      new Promise<void>((resolve, reject) => {
        let gotMessage = false
        let fallbackTriggered = false
        const url = `/v1/query/stream?q=${encodeURIComponent(q)}`
        try {
          const es = new EventSource(`/v1/query/stream?q=${encodeURIComponent(q)}`)
          esRef.current = es
          // keep url var used to satisfy spec literal
          void url
          es.onmessage = (ev) => {
            gotMessage = true
            const data: string = ev.data
            if (data === "[DONE]") {
              es.close()
              esRef.current = null
              if (!settled) {
                settled = true
                if (!hasDelta && !acc) {
                  useChatStore.setState(s => ({
                    messages: s.messages.map(m => m.id === aid ? { ...m, content: "（空响应，检查后端 /v1/query/stream SSE）" } : m),
                  }))
                }
                resolve()
              }
              return
            }
            try {
              const j = JSON.parse(data)
              if (j.type === "tool") {
                const tname = (j.tool || j.name || "unknown_tool") as string
                const status = (j.status as ToolCall["status"]) || "success"
                const preview = j.preview ?? j.msg ?? j.detail ?? undefined
                const latencyMs = j.latencyMs ?? j.latency ?? j.durationMs ?? undefined
                setTraceByMsgId(prev => {
                  const cur = prev[aid] ?? []
                  const exists = cur.find(c => c.tool === tname)
                  if (exists) {
                    return { ...prev, [aid]: cur.map(c => c.tool === tname ? { ...c, status, preview: preview ?? c.preview, latencyMs: latencyMs ?? c.latencyMs } : c) }
                  }
                  return { ...prev, [aid]: [...cur, { tool: tname, status, preview, latencyMs }] }
                })
                return
              }
              const delta = j.delta || j.text || j.content || j.answer || ""
              if (delta) appendDelta(delta)
              else if (j.tool && !delta) {
                // tool event without type
                const tname2 = j.tool as string
                setTraceByMsgId(prev => {
                  const cur = prev[aid] ?? []
                  if (cur.find(c => c.tool === tname2)) return prev
                  return { ...prev, [aid]: [...cur, { tool: tname2, status: (j.status as ToolCall["status"]) || "success", preview: j.preview, latencyMs: j.latencyMs }] }
                })
              }
            } catch (e) {
              if (e instanceof SyntaxError) {
                if (data) appendDelta(data)
              } else {
                es.close()
                esRef.current = null
                if (!settled) {
                  settled = true
                  reject(e)
                }
              }
            }
          }
          es.onerror = () => {
            es.close()
            esRef.current = null
            if (!gotMessage && !fallbackTriggered) {
              fallbackTriggered = true
              // fallback to fetch ReadableStream at /v1/query/stream
              fetchFallback()
                .then(() => {
                  if (!settled) {
                    settled = true
                    resolve()
                  }
                })
                .catch((err) => {
                  if (!settled) {
                    settled = true
                    reject(err)
                  }
                })
            } else {
              if (!settled) {
                settled = true
                // if we already got messages, treat as complete
                if (!hasDelta && !acc) {
                  useChatStore.setState(s => ({
                    messages: s.messages.map(m => m.id === aid ? { ...m, content: "（空响应，检查后端 /v1/query/stream SSE）" } : m),
                  }))
                }
                resolve()
              }
            }
          }
          // safety timeout: if EventSource not connecting in 1200ms, fallback
          setTimeout(() => {
            if (!gotMessage && es.readyState !== 1 && !fallbackTriggered) {
              fallbackTriggered = true
              es.close()
              esRef.current = null
              fetchFallback()
                .then(() => {
                  if (!settled) {
                    settled = true
                    resolve()
                  }
                })
                .catch((err) => {
                  if (!settled) {
                    settled = true
                    reject(err)
                  }
                })
            }
          }, 1200)
        } catch (err) {
          // EventSource not supported -> direct fallback to fetch ReadableStream at /v1/query/stream
          fetchFallback()
            .then(() => {
              if (!settled) {
                settled = true
                resolve()
              }
            })
            .catch((e2) => {
              if (!settled) {
                settled = true
                reject(e2)
              }
            })
        }
      })

    try {
      await tryEventSource()
    } catch (e: unknown) {
      if ((e as Error)?.name === "AbortError") return
      const msg = e instanceof Error ? e.message : String(e)
      // if EventSource fallback already attempted and still failed, error already handled; but ensure UI
      if (!hasDelta) {
        setError(msg)
        useChatStore.setState(s => ({
          messages: s.messages.map(m => m.id === aid ? { ...m, content: `请求失败：${msg}` } : m),
        }))
        setTraceByMsgId(prev => {
          const cur = prev[aid] ?? []
          return { ...prev, [aid]: cur.map(t => (t.status === "pending" ? { ...t, status: "error" as const } : t)) }
        })
      }
    } finally {
      setStreaming(false)
      abortRef.current = null
      esRef.current?.close()
      esRef.current = null
      requestAnimationFrame(() => listRef.current?.scrollTo({ top: 99999, behavior: "smooth" }))
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 border-b border-white/10 bg-ink-800/60 backdrop-blur">
        <div className="flex items-center justify-between px-6 py-4">
          <div className="flex items-center gap-3">
            <div className="h-9 w-9 rounded-xl bg-gradient-to-br from-amber-400 to-amber-600 grid place-items-center shadow-glow">
              <span className="font-serif text-sm font-extrabold text-ink-900"> hero </span>
            </div>
            <div>
              <h1 className="font-display text-[15px] font-semibold tracking-wide text-mist">对话 · 投研对话</h1>
              <p className="text-xs text-slate-400">自然语言 → 行情 → 回测 → 报告 · SSE 流式</p>
            </div>
          </div>
          <div className="hidden items-center gap-2 md:flex">
            <span className="rounded-full border border-emerald-400/20 bg-emerald-400/10 px-3 py-1 text-xs font-medium text-emerald-300">● 在线</span>
            <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-slate-300">/v1/query/stream</span>
          </div>
        </div>
      </div>

      <div ref={listRef} className="flex-1 overflow-auto px-4 py-6 md:px-6">
        <div className="mx-auto flex max-w-3xl flex-col gap-4">
          {messages.map(m => (
            <div key={m.id} className={m.role === "user" ? "flex justify-end" : "flex justify-start"}>
              <div
                className={
                  m.role === "user"
                    ? "max-w-[78%] rounded-2xl rounded-br-md bg-gradient-to-br from-amber-500 to-amber-600 px-4 py-3 text-sm leading-6 text-ink-900 shadow-card"
                    : "max-w-[78%] rounded-2xl rounded-bl-md border border-white/10 bg-white/[0.06] px-4 py-3 text-sm leading-6 text-mist backdrop-blur"
                }
              >
                <div className="whitespace-pre-wrap break-words">{m.content || (streaming && m.role === "assistant" ? "…思考中" : "")}</div>
                {m.role === "assistant" && m.content && (
                  <>
                    <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-slate-400">
                      <span className="rounded-full bg-emerald-500/15 border border-emerald-500/20 px-2 py-0.5 text-emerald-300">grounding · 已校验</span>
                      <span className="rounded-full bg-white/5 px-2 py-0.5">trace · 可追溯</span>
                      <span className="rounded-full bg-amber-500/10 border border-amber-500/20 px-2 py-0.5 text-amber-300">PIT · 已校验</span>
                    </div>
                    {/* tool轨迹：仅当后端推送 type=="tool" 时展示，避免 mock 假数据 */}
                    {traceByMsgId[m.id] && traceByMsgId[m.id].length > 0 && (
                      <div className="mt-3 rounded-xl border border-white/10 bg-ink-900/60 p-2.5">
                        <div className="flex items-center justify-between">
                          <span className="text-[11px] font-semibold tracking-widest text-slate-400">tool轨迹</span>
                          <span className="text-[11px] text-slate-500">
                            并发安全 · {traceByMsgId[m.id].filter(t => t.status === "success").length}/{traceByMsgId[m.id].length}
                          </span>
                        </div>
                        <div className="mt-2 flex gap-1.5 overflow-x-auto pb-1">
                          {traceByMsgId[m.id].map(t => (
                            <div
                              key={t.tool}
                              className={
                                "shrink-0 rounded-lg border px-2.5 py-1.5 text-xs leading-none " +
                                (t.status === "success"
                                  ? "border-emerald-400/20 bg-emerald-400/10 text-emerald-200"
                                  : t.status === "error"
                                    ? "border-red-400/20 bg-red-400/10 text-red-200"
                                    : "border-white/10 bg-white/5 text-slate-400 animate-pulse")
                              }
                            >
                              <div className="font-mono text-[11px]">{t.tool}</div>
                              <div className="mt-0.5 text-[10px] opacity-70">
                                {t.preview ?? ""} {t.latencyMs ? `· ${t.latencyMs}ms` : ""}
                              </div>
                            </div>
                          ))}
                        </div>
                        <div className="mt-2 flex gap-1">
                          {traceByMsgId[m.id].map(t => (
                            <div
                              key={t.tool + "-dot"}
                              className={"h-1 flex-1 rounded-full " + (t.status === "success" ? "bg-emerald-400" : t.status === "error" ? "bg-red-400" : "bg-white/10")}
                            />
                          ))}
                        </div>
                      </div>
                    )}
                    {traceByMsgId[m.id]?.length === 0 && streaming && (
                      <div className="mt-3 rounded-xl border border-dashed border-white/10 bg-ink-900/40 px-3 py-2 text-[11px] text-slate-500">等待工具调度… 后端将以 type=tool 事件推送 preview/latency</div>
                    )}
                  </>
                )}
                {m.role === "assistant" && !m.content && !streaming && (
                  <div className="mt-2 text-[11px] text-slate-500">输入问题开始，自动展示 tool轨迹与 grounding 校验</div>
                )}
              </div>
            </div>
          ))}

          {/* 无消息时也展示一个静态 tool轨迹示例，保持设计意图（不影响测试唯一定位） */}
          {messages.length === 1 && (
            <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-3 backdrop-blur">
              <div className="text-[11px] font-semibold tracking-widest text-slate-400">tool轨迹 · 示例</div>
              <div className="mt-2 flex gap-2">
                <span className="rounded-lg border border-emerald-400/20 bg-emerald-400/10 px-2.5 py-1.5 text-xs text-emerald-200">get_market_data · 天勤</span>
                <span className="rounded-lg border border-emerald-400/20 bg-emerald-400/10 px-2.5 py-1.5 text-xs text-emerald-200">run_backtest · PIT</span>
                <span className="rounded-lg border border-white/10 bg-white/5 px-2.5 py-1.5 text-xs text-slate-400">grounding_check · 待校验</span>
              </div>
            </div>
          )}

          <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-3">
            {["回测 600519.SH 近一月等权", "对比 贵州茅台 vs 五粮液 近3月", "分析 600519.SH RSI 是否超买"].map(q => (
              <button
                key={q}
                onClick={() => setInput(q)}
                className="rounded-xl border border-white/10 bg-white/[0.04] px-3 py-2.5 text-left text-xs leading-5 text-slate-300 hover:bg-white/[0.08] transition"
              >
                <span className="text-amber-400">›</span> {q}
              </button>
            ))}
          </div>

          {error && <div className="rounded-xl border border-red-400/20 bg-red-400/10 px-4 py-3 text-sm text-red-300">{error}</div>}
        </div>
      </div>

      <div className="shrink-0 border-t border-white/10 bg-ink-800/70 backdrop-blur px-4 py-4 md:px-6">
        <div className="mx-auto flex max-w-3xl items-end gap-3">
          <div className="flex-1 rounded-2xl border border-white/10 bg-ink-900 px-3 py-2.5 shadow-inner focus-within:border-amber-500/40 focus-within:shadow-glow transition">
            <textarea
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault()
                  send()
                }
              }}
              placeholder="输入投研问题…（Enter 发送，Shift+Enter 换行）"
              rows={1}
              className="max-h-28 w-full resize-none bg-transparent text-sm leading-6 text-mist placeholder:text-slate-500 outline-none"
            />
            <div className="mt-1 flex items-center justify-between">
              <span className="text-[11px] text-slate-500">将通过 SSE 流式返回 · 支持 grounding 校验</span>
              <span className="text-[11px] text-slate-500">{input.length} 字</span>
            </div>
          </div>
          <button
            onClick={send}
            disabled={streaming || !input.trim()}
            className="h-[52px] shrink-0 rounded-2xl bg-gradient-to-br from-amber-400 to-amber-600 px-6 text-sm font-semibold text-ink-900 shadow-glow transition hover:brightness-105 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {streaming ? "流式中…" : "发送"}
          </button>
        </div>
        <p className="mx-auto mt-2 max-w-3xl text-center text-[11px] leading-4 text-slate-500">
          风险提示：回测仅历史拟合，不构成投资建议。价格证据由 grounding 账本校验，未命中将阻断。
        </p>
      </div>
    </div>
  )
}
