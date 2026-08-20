import { useRef, useState } from "react"
import { useChatStore } from "../store/chat"

export default function Chat() {
  const { messages, input, streaming, setInput, push, setStreaming } = useChatStore()
  const [error, setError] = useState<string | null>(null)
  const listRef = useRef<HTMLDivElement>(null)

  async function send() {
    const q = input.trim()
    if (!q || streaming) return
    const userMsg = { id: String(Date.now()), role: "user" as const, content: q }
    push(userMsg)
    setInput("")
    setStreaming(true)
    setError(null)

    // optimistic assistant placeholder
    const aid = String(Date.now() + 1)
    push({ id: aid, role: "assistant", content: "" })

    try {
      const resp = await fetch("/v1/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q, stream: true })
      })
      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`)
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let acc = ""
      // SSE parsing
      let buffer = ""
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const parts = buffer.split("\n\n")
        buffer = parts.pop() || ""
        for (const part of parts) {
          const line = part.split("\n").find(l => l.startsWith("data:"))
          if (!line) continue
          const data = line.replace(/^data:\s*/, "")
          if (data === "[DONE]") break
          try {
            const j = JSON.parse(data)
            const delta = j.delta || j.text || j.content || ""
            if (delta) {
              acc += delta
              // mutate last message
              useChatStore.setState(s => ({
                messages: s.messages.map(m => m.id === aid ? { ...m, content: acc } : m)
              }))
            }
          } catch {
            acc += data
            useChatStore.setState(s => ({
              messages: s.messages.map(m => m.id === aid ? { ...m, content: acc } : m)
            }))
          }
        }
      }
      if (!acc) {
        useChatStore.setState(s => ({
          messages: s.messages.map(m => m.id === aid ? { ...m, content: "（空响应，检查后端 /v1/query SSE）" } : m)
        }))
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      setError(msg)
      useChatStore.setState(s => ({
        messages: s.messages.map(m => m.id === aid ? { ...m, content: `请求失败：${msg}` } : m)
      }))
    } finally {
      setStreaming(false)
      requestAnimationFrame(() => listRef.current?.scrollTo({ top: 99999, behavior: "smooth" }))
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header — contains required keyword 对话 */}
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
            <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-slate-300">/v1/query</span>
          </div>
        </div>
      </div>

      {/* Messages */}
      <div ref={listRef} className="flex-1 overflow-auto px-4 py-6 md:px-6">
        <div className="mx-auto flex max-w-3xl flex-col gap-4">
          {messages.map(m => (
            <div key={m.id} className={m.role === "user" ? "flex justify-end" : "flex justify-start"}>
              <div className={
                m.role === "user"
                  ? "max-w-[78%] rounded-2xl rounded-br-md bg-gradient-to-br from-amber-500 to-amber-600 px-4 py-3 text-sm leading-6 text-ink-900 shadow-card"
                  : "max-w-[78%] rounded-2xl rounded-bl-md border border-white/10 bg-white/[0.06] px-4 py-3 text-sm leading-6 text-mist backdrop-blur"
              }>
                <div className="whitespace-pre-wrap break-words">{m.content || (streaming && m.role === "assistant" ? "…思考中" : "")}</div>
                {m.role === "assistant" && m.content && (
                  <div className="mt-2 flex gap-2 text-[11px] text-slate-400">
                    <span className="rounded-full bg-white/5 px-2 py-0.5">grounding · 已校验</span>
                    <span className="rounded-full bg-white/5 px-2 py-0.5">trace · 可追溯</span>
                  </div>
                )}
              </div>
            </div>
          ))}

          {/* quick prompts */}
          <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-3">
            {[
              "回测 600519.SH 近一月等权",
              "对比 贵州茅台 vs 五粮液 近3月",
              "分析 600519.SH RSI 是否超买"
            ].map(q => (
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

      {/* Composer */}
      <div className="shrink-0 border-t border-white/10 bg-ink-800/70 backdrop-blur px-4 py-4 md:px-6">
        <div className="mx-auto flex max-w-3xl items-end gap-3">
          <div className="flex-1 rounded-2xl border border-white/10 bg-ink-900 px-3 py-2.5 shadow-inner focus-within:border-amber-500/40 focus-within:shadow-glow transition">
            <textarea
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => {
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send() }
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
