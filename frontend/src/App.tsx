/**
 * 应用装配（App）
 * - Shell 承载全局布局：顶部导航 + 路由内容区 + 底部声明
 * - 路由结构：/ → /dashboard；/research 研究；/backtest 与 /chat 复用 Chat（回测对话）；/live 实盘；/risk 风控；/settings 设置
 * - 页面均懒加载，Suspense 提供加载态，首屏轻量
 */
import { Suspense, lazy, useEffect, useState } from "react"
import { NavLink, Route, Routes, Navigate } from "react-router-dom"
import DegradedBanner from "./components/DegradedBanner"
import { NO_DIST_503_HINT, isServiceUnavailable } from "./config/degraded"

const Dashboard = lazy(() => import("./pages/Dashboard"))
const Research = lazy(() => import("./pages/Research"))
const Chat = lazy(() => import("./pages/Chat"))
const Live = lazy(() => import("./pages/Live"))
const Risk = lazy(() => import("./pages/Risk"))
const Monitor = lazy(() => import("./pages/Monitor"))
const Settings = lazy(() => import("./pages/Settings"))

function Shell() {
  // 全局服务状态横幅：消费 D 线暴露的 `/ready` degraded/503 语义（只读消费，不改 server.py）。
  // 无 dist / 后端降级时后端约定 503；此处做兼容展示：503 或 status!=="ok" 即提示，
  // 若 D 线语义未就绪（/ready 不可达）则同样提示并注明依赖 D 线。
  const [readyNote, setReadyNote] = useState<string | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    fetch("/ready", { cache: "no-store", signal: controller.signal })
      .then(async r => {
        if (controller.signal.aborted) return
        if (isServiceUnavailable(r.status)) {
          let detail = ""
          try {
            const j = (await r.json()) as Record<string, unknown>
            const parts = [`status=${String(j.status ?? "degraded")}`]
            if (j.checkpoint !== undefined) parts.push(`checkpoint=${String(j.checkpoint)}`)
            if (j.billing !== undefined) parts.push(`billing=${String(j.billing)}`)
            detail = parts.join(" · ")
          } catch {
            detail = "status=degraded"
          }
          setReadyNote(`${NO_DIST_503_HINT}（/ready 503 · ${detail}）`)
          return
        }
        if (!r.ok) return
        try {
          const j = (await r.json()) as Record<string, unknown>
          if (j.status !== undefined && j.status !== "ok") {
            const reason = typeof j.status === "string" ? j.status : JSON.stringify(j.status)
            setReadyNote(`后端处于降级状态（/ready status=${reason}），部分数据可能为合成占位。依赖D线：待 degraded:true+reason 落地后横幅将直显后端原因。`)
          }
        } catch {
          // 非 JSON 探活响应忽略，保持无横幅
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setReadyNote(`后端探活不可达（/ready fetch 失败）。${NO_DIST_503_HINT}`)
        }
      })
    return () => controller.abort()
  }, [])

  // 导航激活态：选中用白底深字突出，未选中用半透明文字 + hover 提亮，保持深墨基底对比
  const linkCls = ({ isActive }: { isActive: boolean }) =>
    isActive
      ? "rounded-xl bg-white text-ink-900 px-3.5 py-2 text-sm font-semibold shadow"
      : "rounded-xl px-3.5 py-2 text-sm font-medium text-slate-300 hover:bg-white/10 hover:text-mist transition"

  return (
    <div className="min-h-screen">
      {/* 顶部导航：品牌标识 + 主导航 + 状态/设置入口 */}
      <header className="sticky top-0 z-20 border-b border-white/10 bg-ink-900/75 backdrop-blur-xl">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-3 px-4 py-3 md:px-6">
          <div className="flex items-center gap-3 shrink-0">
            <div className="h-8 w-8 rounded-lg bg-gradient-to-br from-amber-400 to-amber-600 grid place-items-center shadow-glow">
              <span className="font-serif text-xs font-extrabold text-ink-900">HQ</span>
            </div>
            <div className="leading-none">
              <div className="font-serif text-sm font-extrabold tracking-wide text-mist">真英雄量化</div>
              <div className="font-mono text-[10px] tracking-[0.18em] text-slate-400">HERO QUANT</div>
            </div>
            <span className="ml-2 hidden items-center gap-1.5 rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-xs text-slate-300 md:inline-flex"><span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse shadow-[0_0_6px_rgba(52,211,153,0.8)]" />0.2.0 · demo-ready</span>
          </div>

          <nav aria-label="Primary" className="flex max-w-[60vw] items-center gap-1 overflow-x-auto rounded-2xl border border-white/10 bg-white/[0.04] p-1 backdrop-blur scrollbar-none md:max-w-none">
            <NavLink to="/dashboard" className={linkCls}>看板</NavLink>
            <NavLink to="/research" className={linkCls}>研究</NavLink>
            <NavLink to="/backtest" className={linkCls}>回测</NavLink>
            <NavLink to="/live" className={linkCls}>实盘</NavLink>
            <NavLink to="/monitor" className={linkCls}>监控</NavLink>
            <NavLink to="/risk" className={linkCls}>风控</NavLink>
          </nav>

          <div className="hidden items-center gap-2 md:flex shrink-0">
            <span className="text-xs text-slate-400">FastAPI · SSE</span>
            <span className="h-2 w-2 rounded-full bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.8)] animate-pulse" />
            <NavLink to="/settings" className="ml-1 rounded-xl border border-white/10 bg-white/5 px-2.5 py-1.5 text-xs text-slate-300 hover:bg-white/10 transition">设置</NavLink>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl min-h-[calc(100vh-64px)]">
        {/* 全局降级/503 横幅：仅消费后端语义，平时不打扰 */}
        {readyNote ? (
          <div className="px-4 pt-4 md:px-6">
            <DegradedBanner reason={readyNote} source="/ready" />
          </div>
        ) : null}
        {/* 懒加载兜底：居中骨架 + 脉冲，避免白屏 */}
        <Suspense fallback={<div className="p-8"><div className="mx-auto max-w-3xl rounded-2xl border border-white/10 bg-white/[0.03] p-6 animate-pulse"><div className="h-4 w-28 rounded bg-white/10" /><div className="mt-4 h-24 rounded-xl bg-white/5" /><div className="mt-3 h-3 w-1/2 rounded bg-white/5" /></div><p className="mt-3 text-center text-xs text-slate-500">加载中…</p></div>}>
          <Routes>
            <Route path="/" element={<Navigate to="/dashboard" replace />} />
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/research" element={<Research />} />
            {/* 回测与对话复用同一 Chat 组件，/backtest 为历史入口兼容 */}
            <Route path="/backtest" element={<Chat />} />
            <Route path="/chat" element={<Chat />} />
            <Route path="/live" element={<Live />} />
            <Route path="/monitor" element={<Monitor />} />
            <Route path="/risk" element={<Risk />} />
            <Route path="/settings" element={<Settings />} />
          </Routes>
        </Suspense>
      </main>

      <footer className="border-t border-white/5 py-4 text-center text-xs text-slate-500">
        证据优先 · PIT 已校验 · Kepler — 不构成投资建议
      </footer>
    </div>
  )
}

export default function App() {
  return <Shell />
}
