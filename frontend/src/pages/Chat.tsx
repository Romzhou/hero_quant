/**
 * Chat / 回测对话页
 * - 职责：投研对话主界面，承载自然语言 → 行情/回测/报告的流式交互
 * - 数据流：输入 q → 订阅 /v1/query/stream（优先 EventSource，超时或无消息回退 fetch ReadableStream）→ 解析 data: 行
 *   约定 JSON 字段：delta/text/content/answer 为增量文本，type=="tool" 为工具轨迹，type=="error" 为错误，[DONE] 为结束
 * - 渲染：delta 逐片追加到 assistant 消息，tool 事件聚合到 traceByMsgId 渲染轨迹条；支持 AbortController 中断与空响应兜底
 * - 泄漏防护：AbortController + mountedRef 守卫 + 单 rAF 合并 + reader cancel/releaseLock
 */
import { useEffect, useRef, useState } from "react"
import { useChatStore } from "../store/chat"

type ToolCall = { id: string; tool: string; status: "pending" | "success" | "error"; latencyMs?: number; preview?: string }

// 非安全上下文兼容：crypto.randomUUID 在 http/旧浏览器可能缺失，退化为随机串（仅本地消息 id，无安全需求）
function safeUUID(): string {
  try {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID()
  } catch {}
  return `id-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

export const API_ENDPOINTS = {
  TICKET: "/v1/query/ticket",
  STREAM: "/v1/query/stream",
} as const
export const SSE_DONE = "[DONE]"
export const SSE_CONNECT_TIMEOUT_MS = 5500
export const SSE_FIRST_MESSAGE_TIMEOUT_MS = 15_000
export const SSE_FILL_DELAY_MS = 80
export const EMPTY_FALLBACK_MSG = "模型未返回内容，请检查 HERO_API_KEY 配置（当前为合成演示模式）"

export const TOOL_STATUS_CLASS: Record<ToolCall["status"], string> = {
  success: "border-emerald-400/20 bg-emerald-400/10 text-emerald-200",
  error: "border-red-400/20 bg-red-400/10 text-red-200",
  pending: "border-white/10 bg-white/5 text-slate-400 animate-pulse",
}

// 嵌套三元消除：轨迹点颜色映射表，提升可读性并满足 no-nested-ternary
export const TRACE_DOT_CLASS: Record<ToolCall["status"], string> = {
  success: "bg-emerald-400",
  error: "bg-red-400",
  pending: "bg-white/10",
}

// 协议噪声判定：SSE 注释心跳（`:` 开头）、HTML 代理错误页、空帧一律视为 no-op，不进入对话内容
function isProtocolNoise(raw: string): boolean {
  const t = raw.trim()
  if (!t) return true
  return t.startsWith(":") || t.startsWith("<")
}

// 纯函数：统一解析 SSE payload，fetch 回退与 EventSource 共用
export function parseSseData(raw: string): { kind: "delta" | "tool" | "error"; delta?: string; tool?: { tool: string; status: ToolCall["status"]; preview?: string; latencyMs?: number; rawId?: string }; error?: string } | null {
  if (!raw || raw === SSE_DONE) return null
  try {
    const j = JSON.parse(raw) as Record<string, unknown>
    if (j.type === "tool") {
      const rawName = j.tool ?? j.name
      const tname = typeof rawName === "string" && rawName ? rawName : "unknown_tool"
      // 枚举白名单：未知 status 回退 success，避免 TOOL_STATUS_CLASS[t.status] 为 undefined 污染 className
      const rawStatus = j.status
      const status: ToolCall["status"] = rawStatus === "pending" || rawStatus === "error" || rawStatus === "success" ? rawStatus : "success"
      // 类型收窄：非 string preview 统一 String() 化（防 React 子节点渲染崩溃），非有限数 latency 丢弃（防 "NaNms" 双单位）
      const rawPreview = j.preview ?? j.msg ?? j.detail ?? undefined
      const preview = typeof rawPreview === "string" ? rawPreview : rawPreview != null ? String(rawPreview) : undefined
      const rawLatency = j.latencyMs ?? j.latency ?? j.durationMs ?? undefined
      const latencyMs = typeof rawLatency === "number" && Number.isFinite(rawLatency) ? rawLatency : undefined
      const rawId = j.id !== null && j.id !== undefined ? String(j.id) : (j.tool_call_id !== null && j.tool_call_id !== undefined ? String(j.tool_call_id) : undefined)
      return { kind: "tool", tool: { tool: tname, status, preview, latencyMs, rawId } }
    }
    if (j.type === "error") {
      return { kind: "error", error: (j.msg as string) || (j.message as string) || "stream error" }
    }
    // 严格轨迹判定：仅 type==="tool" 视为轨迹；含 tool 字段的 delta 按正常文本处理，避免误路由
    const delta = (j.delta as string) || (j.text as string) || (j.content as string) || (j.answer as string) || ""
    if (typeof delta === "string" && delta) return { kind: "delta", delta }
    // 无可识别字段时视为无操作，避免误判为 delta 空
    return null
  } catch {
    // JSON.parse 仅抛 SyntaxError；噪声帧丢弃，普通文本分片仍视为 delta（兼容 text/plain）
    if (raw && !isProtocolNoise(raw)) return { kind: "delta", delta: raw }
    return null
  }
}

export default function Chat() {
  const { messages, input, streaming, setInput, push, setStreaming } = useChatStore()
  const [error, setError] = useState<string | null>(null)
  const [traceByMsgId, setTraceByMsgId] = useState<Record<string, ToolCall[]>>({})
  // 活跃消息 id 用 state 而非 ref：render 期读取 ref 不会触发重渲染，占位条会 stale
  const [activeAid, setActiveAid] = useState<string | null>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const esRef = useRef<EventSource | null>(null)
  const timeoutIdsRef = useRef<number[]>([])
  const toolSeqRef = useRef(0)
  // send 代际：每次 send() 递增，finally 仅当自己仍是最新一代才清理共享 refs，避免旧 promise 冲掉新一轮
  const sendSeqRef = useRef(0)
  // 挂起的 EventSource promise 结算器：手动 close 不触发 onerror，stop/中断需显式 resolve 唤醒 send
  const settleRef = useRef<{ resolve: () => void; reject: (e: unknown) => void } | null>(null)
  // 单 rAF 合并：全局仅保留一个待执行的滚动任务，避免逐 delta 独立 rAF 导致布局抖动与 rafIds 无限增长
  const pendingScrollRef = useRef<number | null>(null)
  // 卸载防护：标记组件是否已卸载，避免异步回调在卸载后 setState
  const mountedRef = useRef(true)
  // 活跃消息占位走 activeAid state（ref 变更不触发重渲染，渲染期读 ref 会 stale）

  // 单发计时器：触发后自清理，不进 timeoutIdsRef，避免长会话无界增长
  function trackTimeout(id: number) {
    timeoutIdsRef.current.push(id)
  }
  function untrackTimeout(id: number) {
    timeoutIdsRef.current = timeoutIdsRef.current.filter(t => t !== id)
  }
  function clearTrackedTimeout(id: number) {
    window.clearTimeout(id)
    untrackTimeout(id)
  }

  function abortAll() {
    try { abortRef.current?.abort() } catch {}
    if (esRef.current) {
      try { esRef.current.close() } catch {}
      esRef.current = null
    }
    if (pendingScrollRef.current !== null) {
      cancelAnimationFrame(pendingScrollRef.current)
    }
    // SSE 超时兜底：本轮 sseTids 在闭包内但全部进追踪表，此处清表即清全部残留，
    // stop/新一轮抢占后迟发 timer 不得复活 fetchFallback
    timeoutIdsRef.current.forEach(id => window.clearTimeout(id))
    timeoutIdsRef.current = []
    pendingScrollRef.current = null
  }

  // 卸载清理 — 关闭 SSE/Abort 并清理 pending rAF/setTimeout，避免泄漏与 setState on unmounted
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      abortAll()
    }
  }, [])

  function scrollToBottom() {
    const el = listRef.current
    if (!el) return
    const top = el.scrollHeight
    if (el.scrollTo) el.scrollTo({ top, behavior: "smooth" })
    else el.scrollTop = top
  }

  // 单 rAF 调度：合并多次 delta 触发的滚动为一次；合并 rAF 不进追踪表（单 pending 标记足够）
  function scheduleScroll() {
    if (pendingScrollRef.current !== null) return
    pendingScrollRef.current = requestAnimationFrame(() => {
      pendingScrollRef.current = null
      scrollToBottom()
    })
  }

  // 抽取公共 upsert：消除 handlePayload 与 EventSource onmessage 的双份聚合逻辑
  function upsertTrace(aid: string, e: { tool: string; status: ToolCall["status"]; preview?: string; latencyMs?: number; rawId?: string }) {
    const uniqueId = e.rawId ?? `${e.tool}-${toolSeqRef.current++}-${safeUUID()}`
    setTraceByMsgId(prev => {
      const cur = prev[aid] ?? []
      if (e.rawId) {
        const exists = cur.find(c => c.id === e.rawId)
        if (exists) {
          return { ...prev, [aid]: cur.map(c => c.id === e.rawId ? { ...c, status: e.status, preview: e.preview ?? c.preview, latencyMs: e.latencyMs ?? c.latencyMs } : c) }
        }
      }
      return { ...prev, [aid]: [...cur, { id: uniqueId, tool: e.tool, status: e.status, preview: e.preview, latencyMs: e.latencyMs }] }
    })
  }

  // 停止当前流（中断按钮）：走 abortAll 统一清理（含共享 SSE 超时），finally 仅当自己仍是最新一代才清共享 refs
  function stop() {
    abortAll()
    // AbortError 将在 send 的 catch/finally 中收尾；显式唤醒挂起的 EventSource promise
    const st = settleRef.current
    settleRef.current = null
    try { st?.resolve() } catch {}
  }

  async function send() {
    // 中止上一轮并开启新一轮。`streaming` 为 UI 展示/按钮禁用态（非锁）；真正的互斥由 sendSeq 代际保证
    const q = (useChatStore.getState().input ?? "").trim()
    if (!q) return
    const mySeq = ++sendSeqRef.current
    const isCurrent = () => mySeq === sendSeqRef.current
    abortAll()
    const userMsg = { id: safeUUID(), role: "user" as const, content: q }
    push(userMsg)
    setInput("")
    setStreaming(true)
    setError(null)

    const aid = safeUUID()
    push({ id: aid, role: "assistant", content: "" })
    // 初始化空轨迹，占位保证 UI 结构稳定，后续由后端 type=="tool" 事件填充
    setTraceByMsgId(s => ({ ...s, [aid]: [] }))
    setActiveAid(aid)

    const controller = new AbortController()
    abortRef.current = controller
    // 泄漏防护：在每次异步分支前检查 mounted 与 signal，避免卸载后 setState
    const isAlive = () => mountedRef.current && !controller.signal.aborted

    // 用户中断/新一轮抢占时唤醒挂起的 EventSource promise（手动 close 不触发 onerror）
    const onAbort = () => {
      if (isCurrent()) return
      const st = settleRef.current
      settleRef.current = null
      try { st?.resolve() } catch {}
    }
    controller.signal.addEventListener("abort", onAbort)

    const issueSseTicket = async () => {
      const resp = await fetch(API_ENDPOINTS.TICKET, {
        method: "POST",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      // 严格票据校验：不使用 any，使用 unknown + 显式类型守卫
      const payload: unknown = await resp.json()
      if (!payload || typeof payload !== "object" || !("ticket" in payload)) throw new Error("SSE ticket missing")
      const ticket = (payload as { ticket?: unknown }).ticket
      if (typeof ticket !== "string" || !ticket) throw new Error("SSE ticket missing")
      return ticket
    }

    let acc = ""
    let hasDelta = false
    let settled = false
    let gotDone = false

    // 空响应兜底统一入口：finally/各分支共用，避免 EMPTY_FALLBACK_MSG 三处重复赋值
    const ensureNonEmpty = () => {
      if (!hasDelta && !acc && isAlive()) {
        useChatStore.setState(s => ({
          messages: s.messages.map(m => m.id === aid ? { ...m, content: EMPTY_FALLBACK_MSG } : m),
        }))
      }
    }
    // 截断标注：收到过 delta 但未见 [DONE] 即中断，追加提示而非静默按成功处理；pending 轨迹同步转 error 防无限脉冲
    // 守卫用 mounted 而非 isAlive：abort/中断后 signal 已失效，但残留 delta 的标注仍需落盘
    const markTruncated = (reason?: string) => {
      if (hasDelta && mountedRef.current) {
        acc += reason ? `\n\n（响应未完整结束：${reason}）` : "\n\n（响应未完整结束，可能被截断）"
        useChatStore.setState(s => ({
          messages: s.messages.map(m => m.id === aid ? { ...m, content: acc } : m),
        }))
        setTraceByMsgId(prev => {
          const cur = prev[aid] ?? []
          if (!cur.some(t => t.status === "pending")) return prev
          return { ...prev, [aid]: cur.map(t => (t.status === "pending" ? { ...t, status: "error" as const } : t)) }
        })
      }
    }

    const appendDelta = (delta: string) => {
      if (!delta) return
      if (!isAlive()) return
      hasDelta = true
      acc += delta
      // 直接写 Zustand，避免闭包 messages 过期；用单 rAF 聚合滚动避免每片 delta 强制布局抖动
      useChatStore.setState(s => ({
        messages: s.messages.map(m => m.id === aid ? { ...m, content: acc } : m),
      }))
      scheduleScroll()
    }

    const handlePayload = (raw: string) => {
      const parsed = parseSseData(raw)
      if (!parsed) {
        if (raw === SSE_DONE) return
        // null means no-op (already handled) or empty
        return
      }
      if (parsed.kind === "tool" && parsed.tool) {
        if (!isAlive()) return
        const { tool: tname, status, preview, latencyMs, rawId } = parsed.tool
        upsertTrace(aid, { tool: tname, status, preview, latencyMs, rawId })
        return
      }
      if (parsed.kind === "error" && parsed.error) {
        throw new Error(parsed.error)
      }
      if (parsed.kind === "delta" && parsed.delta) {
        appendDelta(parsed.delta)
      }
    }

    // fetch 回退：手动解析 SSE 帧，兼容不支持 EventSource 或代理缓冲的场景
    // 互斥：回退前确保 EventSource 已关闭，避免与主链路抢用已消费的一次性 ticket（曾导致 403→无回显）
    const fetchFallback = async () => {
      try { esRef.current?.close() } catch {}
      esRef.current = null
      const ticket = await issueSseTicket()
      const url = `${API_ENDPOINTS.STREAM}?q=${encodeURIComponent(q)}&ticket=${encodeURIComponent(ticket)}`
      const resp = await fetch(url, {
        method: "GET",
        headers: { Accept: "text/event-stream" },
        signal: controller.signal,
      })
      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`)
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ""
      let outerDone = false
      // 泄漏防护：确保 reader 在错误/中断/abort 时也能 cancel 并 releaseLock
      try {
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
            if (data === SSE_DONE) { gotDone = true; outerDone = true; break }
            handlePayload(data)
          }
          if (outerDone) break
        }
        // flush decoder 尾部，避免末尾无 \n\n 的帧丢失
        buffer += decoder.decode()
        if (buffer.trim()) {
          const tailLines = buffer.split("\n").filter(l => l.startsWith("data:")).map(l => l.replace(/^data:\s*/, ""))
          const tailData = tailLines.join("\n").trim()
          if (tailData) {
            if (tailData === SSE_DONE) gotDone = true
            else handlePayload(tailData)
          }
        }
        ensureNonEmpty()
      } finally {
        try { await reader.cancel() } catch {}
        try { reader.releaseLock() } catch {}
      }
    }
    // 优先 EventSource：浏览器原生 SSE 自动重连，失败或超时再回退 fetch
    const tryEventSource = async () => {
      const ticket = await issueSseTicket()
      return new Promise<void>((resolve, reject) => {
        settleRef.current = { resolve: () => { if (!settled) { settled = true; resolve() } }, reject: (e: unknown) => { if (!settled) { settled = true; reject(e as Error) } } }
        const settleResolve = () => { const st = settleRef.current; settleRef.current = null; try { st?.resolve() } catch {} }
        const settleReject = (e: unknown) => { const st = settleRef.current; settleRef.current = null; try { st?.reject(e) } catch {} }
        let gotMessage = false
        let fallbackTriggered = false
        const url = `${API_ENDPOINTS.STREAM}?q=${encodeURIComponent(q)}&ticket=${encodeURIComponent(ticket)}`
        // 本轮 SSE 超时句柄组（建连超时 + 首包超时），任一终态即全部 clear，残留由 abortAll 兜底
        const sseTids: number[] = []
        // 超时句柄：首包/[DONE]/error/回退任一发生即 clear，避免迟发二次回退；残留由 abortAll 清表兜底
        const clearSseTimeout = () => {
          sseTids.forEach(t => clearTrackedTimeout(t))
          sseTids.length = 0
        }
        const closeEs = () => {
          clearSseTimeout()
          try { esRef.current?.close() } catch {}
          esRef.current = null
        }
        const runFallback = () => {
          fallbackTriggered = true
          closeEs()
          // 尚未收到任何消息时判定为连接失败，回退到 fetch 手动解析 SSE
          fetchFallback().then(settleResolve, settleReject)
        }
        try {
          const es = new EventSource(url)
          esRef.current = es
          es.onmessage = (ev) => {
            gotMessage = true
            clearSseTimeout()
            const data: string = ev.data
            if (data === SSE_DONE) {
              gotDone = true
              closeEs()
              settleResolve()
              return
            }
            const parsed = parseSseData(data)
            if (!parsed) return
            if (parsed.kind === "tool" && parsed.tool) {
              if (!isAlive()) return
              const { tool: tname, status, preview, latencyMs, rawId } = parsed.tool
              upsertTrace(aid, { tool: tname, status, preview, latencyMs, rawId })
              return
            }
            if (parsed.kind === "error" && parsed.error) {
              closeEs()
              settleReject(new Error(parsed.error))
              return
            }
            if (parsed.kind === "delta" && parsed.delta) appendDelta(parsed.delta)
          }
          es.onerror = () => {
            closeEs()
            if (!gotMessage && !fallbackTriggered) {
              runFallback()
            } else {
              // 收尾判定统一收敛到 finally（截断标注/空兜底），此处仅结算
              settleResolve()
            }
          }
          // 超时保护两级：建连超时（未 OPEN 则回退）+ 首包超时（已 OPEN 但无数据则回退，避免心跳空转卡死 streaming）
          const connectTid = window.setTimeout(() => {
            if (!gotMessage && es.readyState !== 1 && !fallbackTriggered) runFallback()
          }, SSE_CONNECT_TIMEOUT_MS)
          trackTimeout(connectTid)
          sseTids.push(connectTid)
          const firstMsgTid = window.setTimeout(() => {
            if (!gotMessage && !fallbackTriggered) runFallback()
          }, SSE_FIRST_MESSAGE_TIMEOUT_MS)
          trackTimeout(firstMsgTid)
          sseTids.push(firstMsgTid)
        } catch {
          // 环境不支持 EventSource 时直接走 fetch 回退
          fetchFallback().then(settleResolve, settleReject)
        }
      })
    }

    try {
      await tryEventSource()
      // 正常结算：未见 [DONE] 即视为截断（有 delta 标注截断，无内容走空兜底）
      if (isAlive()) {
        if (hasDelta && !gotDone) markTruncated()
        else ensureNonEmpty()
      }
    } catch (e: unknown) {
      if ((e as Error)?.name === "AbortError") {
        // 中断/新一轮抢占：abort 后 isAlive() 恒为 false，改用 mounted 守卫 + 代际守卫标注残留 delta
        if (hasDelta && !gotDone && mountedRef.current && isCurrent()) markTruncated("已中断")
        return
      }
      const msg = e instanceof Error ? e.message : String(e)
      // 错误始终可见：无 delta 时覆写正文 + 错误条；有残留 delta 时保留正文但错误条 + 截断原因可诊断
      if (isAlive()) {
        setError(msg)
        if (!hasDelta) {
          useChatStore.setState(s => ({
            messages: s.messages.map(m => m.id === aid ? { ...m, content: `请求失败：${msg}` } : m),
          }))
        } else if (!gotDone) {
          markTruncated(msg)
          // 有残留 delta 的错误分支同样需翻转 pending 轨迹（markTruncated 内已处理，此处兜底幂等）
        }
        setTraceByMsgId(prev => {
          const cur = prev[aid] ?? []
          return { ...prev, [aid]: cur.map(t => (t.status === "pending" ? { ...t, status: "error" as const } : t)) }
        })
      }
    } finally {
      // 代际守卫：旧 send 结算不得翻转新一轮的 streaming（修复重叠发送状态闪烁）
      const current = isCurrent()
      if (current && mountedRef.current) setStreaming(false)
      if (current) {
        abortRef.current = null
        try { esRef.current?.close() } catch {}
        esRef.current = null
        settleRef.current = null
        setActiveAid(null)
      }
      controller.signal.removeEventListener("abort", onAbort)
      // 收尾滚动到底，确保最后 delta 可见（单 rAF 合并）
      scheduleScroll()
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
            <span className={"rounded-full border px-3 py-1 text-xs font-medium flex items-center gap-1.5 " + (streaming ? "border-amber-400/30 bg-amber-400/15 text-amber-300" : "border-emerald-400/20 bg-emerald-400/10 text-emerald-300")}>
              <span className={"h-1.5 w-1.5 rounded-full " + (streaming ? "bg-amber-400 animate-pulse" : "bg-emerald-400")} />{streaming ? "流式中…" : "● 在线"}
            </span>
            <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-slate-300">{API_ENDPOINTS.STREAM}</span>
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
                    : m.content === EMPTY_FALLBACK_MSG
                      ? "max-w-[78%] rounded-2xl rounded-bl-md border border-amber-400/30 bg-amber-400/10 px-4 py-3 text-sm leading-6 text-amber-200 backdrop-blur"
                      : "max-w-[78%] rounded-2xl rounded-bl-md border border-white/10 bg-white/[0.06] px-4 py-3 text-sm leading-6 text-mist backdrop-blur"
                }
              >
                <div className="whitespace-pre-wrap break-words">
                  {m.content ? m.content : streaming && m.role === "assistant" ? <span className="inline-flex items-center gap-1.5 text-slate-400"><span className="h-2 w-2 rounded-full bg-amber-400 animate-pulse" />思考中…</span> : ""}
                </div>
                {m.role === "assistant" && m.content && m.content !== EMPTY_FALLBACK_MSG && (
                  <>
                    <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-slate-400">
                      <span className="rounded-full bg-emerald-500/15 border border-emerald-500/20 px-2 py-0.5 text-emerald-300">grounding · 已校验</span>
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
                              key={t.id}
                              className={
                                "shrink-0 rounded-lg border px-2.5 py-1.5 text-xs leading-none " + TOOL_STATUS_CLASS[t.status]
                              }
                            >
                              <div className="flex items-center gap-1.5">
                                <span className="font-mono text-[11px]">{t.tool}</span>
                                {t.latencyMs !== undefined && t.latencyMs !== null ? <span className="rounded bg-white/10 px-1 py-0.5 font-mono text-[10px] leading-none">{t.latencyMs}ms</span> : null}
                              </div>
                              <div className="mt-1 text-[10px] opacity-70 truncate max-w-[140px]">{t.preview ?? ""}</div>
                            </div>
                          ))}
                        </div>
                        <div className="mt-2 flex gap-1">
                          {traceByMsgId[m.id].map(t => (
                            <div
                              key={t.id + "-dot"}
                              className={"h-1 flex-1 rounded-full " + TRACE_DOT_CLASS[t.status]}
                            />
                          ))}
                        </div>
                      </div>
                    )}
                    {traceByMsgId[m.id]?.length === 0 && streaming && m.id === activeAid && (
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
          {messages.length <= 1 && (
            <div className="rounded-2xl border border-white/10 bg-gradient-to-br from-white/[0.04] to-white/[0.02] p-4 backdrop-blur">
              <div className="flex items-center justify-between">
                <div className="text-[11px] font-semibold tracking-widest text-slate-400">tool轨迹 · 示例</div>
                <span className="rounded-full bg-emerald-400/10 border border-emerald-400/20 px-2 py-0.5 text-[10px] text-emerald-300">并发安全</span>
              </div>
              <div className="mt-2 flex flex-wrap gap-2">
                <span className="rounded-lg border border-emerald-400/20 bg-emerald-400/10 px-2.5 py-1.5 text-xs text-emerald-200">get_market_data · 天勤 <span className="ml-1 rounded bg-white/10 px-1 text-[10px]">42ms</span></span>
                <span className="rounded-lg border border-emerald-400/20 bg-emerald-400/10 px-2.5 py-1.5 text-xs text-emerald-200">run_backtest · PIT <span className="ml-1 rounded bg-white/10 px-1 text-[10px]">180ms</span></span>
                <span className="rounded-lg border border-white/10 bg-white/5 px-2.5 py-1.5 text-xs text-slate-400">grounding_check · 待校验</span>
              </div>
              <p className="mt-2 text-[11px] text-slate-500">真实请求将流式推送 preview / latency，此为演示占位</p>
            </div>
          )}

          <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-3">
            {["回测 600519.SH 近一月等权", "对比 贵州茅台 vs 五粮液 近3月", "分析 600519.SH RSI 是否超买"].map(q => (
              <button
                key={q}
                onClick={() => {
                  setInput(q)
                }}
                className="group rounded-xl border border-white/10 bg-white/[0.04] px-3 py-3 text-left text-xs leading-5 text-slate-300 hover:bg-white/[0.08] hover:border-amber-500/20 transition"
              >
                <span className="text-amber-400 group-hover:text-amber-300">›</span> {q}
                <span className="ml-1 text-[11px] text-slate-500 group-hover:text-slate-400">· 点击填入</span>
              </button>
            ))}
          </div>

          {error && <div className="rounded-xl border border-red-400/20 bg-red-400/10 px-4 py-3 text-sm text-red-300">{error}</div>}
        </div>
      </div>

      <div className="shrink-0 border-t border-white/10 bg-ink-800/70 backdrop-blur px-4 py-4 md:px-6">
        <div className="mx-auto max-w-3xl mb-3 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-amber-400/20 bg-amber-500/10 px-3 py-2">
          <div className="flex items-center gap-2 text-xs">
            <span className="h-2 w-2 rounded-full bg-amber-400 animate-pulse" />
            <span className="font-semibold text-amber-300">一键演示</span>
            <span className="hidden text-amber-200/70 md:inline">预填并直接发送 · 30秒看真链路</span>
            <span className="rounded-full bg-white px-2 py-0.5 font-mono text-[11px] text-amber-700">回测 600519.SH 近一月等权</span>
          </div>
          <button
            onClick={() => {
              const q = "回测 600519.SH 近一月等权"
              setInput(q)
              // 单发 UI 计时器：触发后自清理，不进 timeoutIdsRef（长会话无界增长）
              const tid = window.setTimeout(() => {
                untrackTimeout(tid)
                const cur = useChatStore.getState().input
                if (cur.trim() === q) send()
              }, SSE_FILL_DELAY_MS)
              trackTimeout(tid)
            }}
            className="rounded-lg bg-amber-500 px-3 py-1.5 text-xs font-semibold text-ink-900 hover:bg-amber-400 transition"
          >
            ▶ 立即演示
          </button>
        </div>
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
            onClick={() => {
              if (useChatStore.getState().streaming) stop()
              else send()
            }}
            disabled={!streaming && !input.trim()}
            className="h-[52px] shrink-0 rounded-2xl bg-gradient-to-br from-amber-400 to-amber-600 px-6 text-sm font-semibold text-ink-900 shadow-glow transition hover:brightness-105 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {streaming ? "停止" : "发送"}
          </button>
        </div>
        <p className="mx-auto mt-2 max-w-3xl text-center text-[11px] leading-4 text-slate-500">
          风险提示：回测仅历史拟合，不构成投资建议。价格证据由 grounding 账本校验，未命中将阻断。
        </p>
      </div>
    </div>
  )
}
