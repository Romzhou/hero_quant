/**
 * Settings 设置页
 * - 职责：本地偏好配置（仅浏览器存储，不上传服务端）—— API 基地址与模型选择
 * - 数据流：读写 Zustand settings store；apiBase 为空时走 Vite 同源代理 /v1，填写后可直连远端网关
 * - 自检：挂载时探测 /live 与 /v1/backtest/metrics.json，展示连通性与时延，便于演示排错
 */
import { useEffect, useState } from "react"
import { useSettingsStore } from "../store/settings"

type Check = { ok: boolean | null; latency?: number; status?: number }

export default function Settings() {
  const { apiBase, model, setApiBase, setModel } = useSettingsStore()
  const [live, setLive] = useState<Check>({ ok: null })
  const [metrics, setMetrics] = useState<Check>({ ok: null })

  useEffect(() => {
    let aborted = false
    async function probe(url: string, setter: (c: Check) => void) {
      const t0 = performance.now()
      try {
        const r = await fetch(url, { cache: "no-store" })
        const dt = Math.round(performance.now() - t0)
        if (!aborted) setter({ ok: r.ok, latency: dt, status: r.status })
      } catch {
        const dt = Math.round(performance.now() - t0)
        if (!aborted) setter({ ok: false, latency: dt })
      }
    }
    probe("/live", setLive)
    probe("/v1/backtest/metrics.json", setMetrics)
    const timer = setInterval(() => {
      probe("/live", setLive)
      probe("/v1/backtest/metrics.json", setMetrics)
    }, 15000)
    return () => { aborted = true; clearInterval(timer) }
  }, [])

  const Dot = ({ ok }: { ok: boolean | null }) => (
    <span className={"h-2 w-2 rounded-full " + (ok === null ? "bg-slate-500 animate-pulse" : ok ? "bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.8)]" : "bg-red-400")} />
  )

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
          <div className="rounded-xl border border-white/10 bg-ink-900/60 px-3 py-3 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Dot ok={live.ok} />
              <span className="font-mono text-xs text-mist">/live</span>
              <span className="text-[11px] text-slate-500">{live.ok === null ? "检测中…" : live.ok ? "连通" : "未连通"}</span>
            </div>
            <span className="font-mono text-xs text-slate-400">{live.latency !== undefined ? `${live.latency}ms` : "—"} {live.status ? `· ${live.status}` : ""}</span>
          </div>
          <div className="rounded-xl border border-white/10 bg-ink-900/60 px-3 py-3 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Dot ok={metrics.ok} />
              <span className="font-mono text-xs text-mist">/v1/backtest/metrics.json</span>
              <span className="text-[11px] text-slate-500">{metrics.ok === null ? "检测中…" : metrics.ok ? "就绪" : "演示回退"}</span>
            </div>
            <span className="font-mono text-xs text-slate-400">{metrics.latency !== undefined ? `${metrics.latency}ms` : "—"} {metrics.status ? `· ${metrics.status}` : ""}</span>
          </div>
        </div>
        <p className="mt-3 text-xs leading-5 text-slate-500">用于演示前快速排错：若显示未连通，请确认后端 <span className="font-mono text-slate-300">uvicorn hero_quant.api.server:app --port 8899</span> 已启动且 Vite proxy 指向 8899。</p>
        <div className="mt-2 flex gap-2">
          <a href="/live" target="_blank" rel="noreferrer" className="rounded-lg border border-white/10 bg-white/5 px-2.5 py-1 text-xs text-mist hover:bg-white/10">打开 /live ↗</a>
          <a href="/v1/backtest/metrics.json" target="_blank" rel="noreferrer" className="rounded-lg border border-amber-500/20 bg-amber-500/10 px-2.5 py-1 text-xs text-amber-300">metrics.json</a>
        </div>
      </div>

      <div className="mt-5 space-y-5">
        <div className="rounded-2xl border border-white/10 bg-ink-800/60 p-5 backdrop-blur">
          <label className="text-xs font-semibold tracking-widest text-slate-400">API 基地址</label>
          <input
            value={apiBase}
            onChange={e => setApiBase(e.target.value)}
            placeholder="留空则同源代理（/v1）· 可填 https://api.example.com"
            className="mt-2 w-full rounded-xl border border-white/10 bg-ink-900 px-3 py-2.5 text-sm text-mist placeholder:text-slate-500 outline-none focus:border-amber-500/40"
          />
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
