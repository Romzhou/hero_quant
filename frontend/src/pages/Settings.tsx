/**
 * Settings 设置页
 * - 职责：本地偏好配置（仅浏览器存储，不上传服务端）—— API 基地址与模型选择
 * - 数据流：读写 Zustand settings store；apiBase 为空时走 Vite 同源代理 /v1，填写后可直连远端网关
 * - 自检：探测 /live 与 /v1/backtest/metrics.json，展示连通性与时延，便于演示排错
 */
import { useEffect, useRef, useState } from "react"
import { useSettingsStore } from "../store/settings"

type Check = { ok: boolean | null; latency?: number; status?: number }

export const ENDPOINTS = {
  LIVE: "/live",
  METRICS: "/v1/backtest/metrics.json",
} as const

export function getDotClass(ok: boolean | null): string {
  if (ok === null) return "bg-slate-500 animate-pulse"
  if (ok) return "bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.8)]"
  return "bg-red-400"
}

export function StatusDot({ ok }: { ok: boolean | null }) {
  return <span className={"h-2 w-2 rounded-full " + getDotClass(ok)} />
}

// 抽取嵌套三元：统一状态文案，避免里层三元可读性差与重复
export function getStatusText(ok: boolean | null, okText: string, failText: string): string {
  if (ok === null) return "检测中…"
  if (ok) return okText
  return failText
}

function resolveUrl(apiBase: string, path: string): string {
  if (!apiBase) return path
  // 修复单斜杠剥离：/https://host// -> //live，故用 /\/+$/ 去除多尾斜杠
  return apiBase.replace(/\/+$/, "") + path
}

// fail-closed 链接构造：apiBase 进入 href 前校验协议，仅 http/https 放行，其余回退相对路径防 javascript: 等
function safeResolveUrl(apiBase: string, path: string): string {
  if (!apiBase) return path
  try {
    const u = new URL(apiBase)
    if (u.protocol !== "http:" && u.protocol !== "https:") return path
  } catch {
    return path
  }
  return resolveUrl(apiBase, path)
}

// 抽取重复卡片：live/metrics 卡片布局、StatusDot、时延/状态渲染一致，抽组件防漂移
function ConnectivityCard({ path, check, okText, failText }: { path: string; check: Check; okText: string; failText: string }) {
  return (
    <div className="rounded-xl border border-white/10 bg-ink-900/60 px-3 py-3 flex items-center justify-between">
      <div className="flex items-center gap-2">
        <StatusDot ok={check.ok} />
        <span className="font-mono text-xs text-mist">{path}</span>
        <span className="text-[11px] text-slate-500">{getStatusText(check.ok, okText, failText)}</span>
      </div>
      <span className="font-mono text-xs text-slate-400">{check.latency !== undefined ? `${check.latency}ms` : "—"} {check.status ? `· ${check.status}` : ""}</span>
    </div>
  )
}

export default function Settings() {
  const { apiBase, model, setApiBase, setModel } = useSettingsStore()
  const [live, setLive] = useState<Check>({ ok: null })
  const [metrics, setMetrics] = useState<Check>({ ok: null })
  const [apiBaseError, setApiBaseError] = useState<string | null>(null)
  const [draftApiBase, setDraftApiBase] = useState(apiBase)

  useEffect(() => { setDraftApiBase(apiBase) }, [apiBase])

  const buildUrl = (path: string) => resolveUrl(apiBase, path)
  // 渲染期 href 必须走 fail-closed 校验，避免 persisted/tampered 的 javascript: 进入 <a href>
  const safeBuildUrl = (path: string) => safeResolveUrl(apiBase, path)

  // generation 锁：避免重叠探活竞态，旧批次响应不覆盖新批次
  const probeSeqRef = useRef(0)
  useEffect(() => {
    const controller = new AbortController()
    const { signal } = controller
    let aborted = false
    const seq = ++probeSeqRef.current
    async function probe(url: string, setter: (c: Check) => void) {
      const t0 = performance.now()
      try {
        const r = await fetch(url, { cache: "no-store", signal } as RequestInit)
        const dt = Math.round(performance.now() - t0)
        // 仅当 generation 仍为当前才写状态，丢弃过期响应
        if (!aborted && !signal.aborted && seq === probeSeqRef.current) setter({ ok: r.ok, latency: dt, status: r.status })
      } catch (e) {
        if (signal.aborted) return
        if (e instanceof DOMException && e.name === "AbortError") return
        const dt = Math.round(performance.now() - t0)
        if (!aborted && !signal.aborted && seq === probeSeqRef.current) setter({ ok: false, latency: dt })
      }
    }
    const doProbe = () => {
      // 并发双探活但受 generation 保护
      probe(buildUrl(ENDPOINTS.LIVE), setLive)
      probe(buildUrl(ENDPOINTS.METRICS), setMetrics)
    }
    doProbe()
    const timer = setInterval(doProbe, 15000)
    return () => { aborted = true; controller.abort(); clearInterval(timer) }
  }, [apiBase])

  // 防抖提交：输入即校验仅改 draft 与错误提示，真正持久化与探活重启走 debounce，避免每键触发两次 fetch
  useEffect(() => {
    const raw = draftApiBase
    const trimmed = raw.trim()
    // 空值：防抖后清空持久化
    if (trimmed === "") {
      // 仅当当前持久化非空时才需提交空值
      if (apiBase !== "") {
        const id = window.setTimeout(() => {
          setApiBaseError(null)
          setApiBase("")
        }, 350)
        return () => clearTimeout(id)
      }
      return
    }
    const normalized = trimmed.replace(/\/+$/, "")
    let valid = false
    try {
      const u = new URL(normalized)
      valid = u.protocol === "http:" || u.protocol === "https:"
    } catch {
      valid = false
    }
    if (!valid) return
    // 已是目标值则不重复提交
    if (normalized === apiBase) return
    const id = window.setTimeout(() => {
      setApiBaseError(null)
      setApiBase(normalized)
    }, 350)
    return () => clearTimeout(id)
  }, [draftApiBase, apiBase, setApiBase])

  const handleApiBaseChange = (raw: string) => {
    // 保留原始输入到 draft，避免 trim 导致光标跳动；仅校验与防抖提交阶段做 trim/normalize
    setDraftApiBase(raw)
    const trimmed = raw.trim()
    if (trimmed === "") {
      setApiBaseError(null)
      return
    }
    const normalized = trimmed.replace(/\/+$/, "")
    try {
      const u = new URL(normalized)
      if (u.protocol !== "http:" && u.protocol !== "https:") throw new Error("invalid protocol")
      setApiBaseError(null)
      // 合法输入不立即污染 persisted apiBase，由防抖 effect 统一提交
    } catch {
      setApiBaseError("无效 URL，需包含 http(s):// 且格式正确")
      // 非法输入仅留在 draft，不污染 persisted apiBase，避免 XSS/open-redirect via href
    }
  }

  // 额外支持 onBlur 立即提交，缩短有效输入的可见延迟
  const handleApiBaseBlur = () => {
    const trimmed = draftApiBase.trim()
    if (trimmed === "") {
      if (apiBase !== "") {
        setApiBaseError(null)
        setApiBase("")
      }
      return
    }
    const normalized = trimmed.replace(/\/+$/, "")
    try {
      const u = new URL(normalized)
      if (u.protocol !== "http:" && u.protocol !== "https:") throw new Error("invalid protocol")
      setApiBaseError(null)
      if (normalized !== apiBase) setApiBase(normalized)
    } catch {
      setApiBaseError("无效 URL，需包含 http(s):// 且格式正确")
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-6">
      <h1 className="font-display text-xl font-semibold text-mist">设置</h1>
      <p className="mt-1 text-sm text-slate-400">本地偏好 · 仅浏览器存储，不上传服务端</p>

      {/* 连接自检 */}
      <div className="mt-6 rounded-2xl border border-white/10 bg-ink-800/60 p-5 backdrop-blur">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-mist">连接自检</h2>
          <span className="text-[11px] text-slate-500">自动探测 · 15s 刷新</span>
        </div>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <ConnectivityCard path={ENDPOINTS.LIVE} check={live} okText="连通" failText="未连通" />
          <ConnectivityCard path={ENDPOINTS.METRICS} check={metrics} okText="就绪" failText="演示回退" />
        </div>
        <p className="mt-3 text-xs leading-5 text-slate-500">用于演示前快速排错：若显示未连通，请确认后端 <span className="font-mono text-slate-300">uvicorn hero_quant.api.server:app --port 8899</span> 已启动且 Vite proxy 指向 8899。</p>
        <div className="mt-2 flex gap-2">
          <a href={safeBuildUrl(ENDPOINTS.LIVE)} target="_blank" rel="noopener noreferrer" className="rounded-lg border border-white/10 bg-white/5 px-2.5 py-1 text-xs text-mist hover:bg-white/10">打开 /live ↗</a>
          <a href={safeBuildUrl(ENDPOINTS.METRICS)} target="_blank" rel="noopener noreferrer" className="rounded-lg border border-amber-500/20 bg-amber-500/10 px-2.5 py-1 text-xs text-amber-300">metrics.json</a>
        </div>
      </div>

      <div className="mt-5 space-y-5">
        <div className="rounded-2xl border border-white/10 bg-ink-800/60 p-5 backdrop-blur">
          <label className="text-xs font-semibold tracking-widest text-slate-400">API 基地址</label>
          <input
            value={draftApiBase}
            onChange={e => handleApiBaseChange(e.target.value)}
            onBlur={handleApiBaseBlur}
            placeholder="留空则同源代理（/v1）· 可填 https://api.example.com"
            className="mt-2 w-full rounded-xl border border-white/10 bg-ink-900 px-3 py-2.5 text-sm text-mist placeholder:text-slate-500 outline-none focus:border-amber-500/40"
          />
          {apiBaseError && <p className="mt-2 text-xs text-red-300">无效 URL：请输入完整地址，例如 https://api.example.com</p>}
          <p className="mt-2 text-xs text-slate-500">用于覆盖 fetch 基路径，默认走 Vite proxy 到 localhost:8899</p>
        </div>

        <div className="rounded-2xl border border-white/10 bg-ink-800/60 p-5 backdrop-blur">
          <label className="text-xs font-semibold tracking-widest text-slate-400">模型</label>
          <select
            value={model}
            onChange={e => setModel(e.target.value)}
            className="mt-2 w-full rounded-xl border border-white/10 bg-ink-900 px-3 py-2.5 text-sm text-mist outline-none focus:border-amber-500/40"
          >
            <option value="gpt-4o-mini">gpt-4o-mini（默认）</option>
            <option value="deepseek-chat">deepseek-chat</option>
            <option value="qwen-plus">qwen-plus</option>
          </select>
        </div>

        <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-5">
          <h2 className="text-sm font-semibold text-mist">关于 · 真英雄量化</h2>
          <p className="mt-2 text-sm leading-6 text-slate-400">
            极简投研 Agent 闭环：自然语言 → 行情（registry + tencent/yahoo）→ 回测（engine + metrics + validation）→ 报告（memory + grounding + trace）。
            前端三页：对话 / 研究 / 设置。设计上采用深墨基底 + 琥珀高光，强调证据与可追溯性。
          </p>
          <div className="mt-3 flex flex-wrap gap-2 text-xs">
            <span className="rounded-full bg-white/5 px-3 py-1 text-slate-300">React 19</span>
            <span className="rounded-full bg-white/5 px-3 py-1 text-slate-300">Zustand</span>
            <span className="rounded-full bg-white/5 px-3 py-1 text-slate-300">ECharts</span>
            <span className="rounded-full bg-white/5 px-3 py-1 text-slate-300">Tailwind</span>
          </div>
        </div>

        <div className="rounded-2xl border border-emerald-400/15 bg-emerald-400/10 px-4 py-3 text-xs leading-5 text-emerald-200">
          后端健康检查：<code className="rounded bg-black/20 px-1.5 py-0.5">/live</code> <code className="rounded bg-black/20 px-1.5 py-0.5">/ready</code> <code className="rounded bg-black/20 px-1.5 py-0.5">/metrics</code> · 前端已做 proxy，生产可配网关鉴权。
        </div>
      </div>
    </div>
  )
}
