import { useSettingsStore } from "../store/settings"

export default function Settings() {
  const { apiBase, model, setApiBase, setModel } = useSettingsStore()

  return (
    <div className="mx-auto max-w-3xl px-6 py-6">
      <h1 className="font-display text-xl font-semibold text-mist">设置</h1>
      <p className="mt-1 text-sm text-slate-400">本地偏好 · 仅浏览器存储，不上传服务端</p>

      <div className="mt-6 space-y-5">
        <div className="rounded-2xl border border-white/10 bg-ink-800/60 p-5 backdrop-blur">
          <label className="text-xs font-semibold tracking-widest text-slate-400">API 基地址</label>
          <input
            value={apiBase}
            onChange={e => setApiBase(e.target.value)}
            placeholder="留空则同源代理（/v1）· 可填 https://api.example.com"
            className="mt-2 w-full rounded-xl border border-white/10 bg-ink-900 px-3 py-2.5 text-sm text-mist placeholder:text-slate-500 outline-none focus:border-amber-500/40"
          />
          <p className="mt-2 text-xs text-slate-500">用于覆盖 fetch 基路径，默认走 Vite proxy 到 localhost:8000</p>
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
