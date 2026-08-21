import { Suspense, lazy } from "react"
import { NavLink, Route, Routes, Navigate } from "react-router-dom"

const Dashboard = lazy(() => import("./pages/Dashboard"))
const Research = lazy(() => import("./pages/Research"))
const Chat = lazy(() => import("./pages/Chat"))
const Live = lazy(() => import("./pages/Live"))
const Risk = lazy(() => import("./pages/Risk"))
const Settings = lazy(() => import("./pages/Settings"))

function Shell() {
  const linkCls = ({ isActive }: { isActive: boolean }) =>
    isActive
      ? "rounded-xl bg-white text-ink-900 px-3.5 py-2 text-sm font-semibold shadow"
      : "rounded-xl px-3.5 py-2 text-sm font-medium text-slate-300 hover:bg-white/10 hover:text-mist transition"

  return (
    <div className="min-h-screen">
      {/* top nav */}
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
            <span className="ml-2 hidden rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-xs text-slate-300 md:inline">0.2.0 · slim</span>
          </div>

          <nav aria-label="Primary" className="flex max-w-[60vw] items-center gap-1 overflow-x-auto rounded-2xl border border-white/10 bg-white/[0.04] p-1 backdrop-blur scrollbar-none md:max-w-none">
            <NavLink to="/dashboard" className={linkCls}>看板</NavLink>
            <NavLink to="/research" className={linkCls}>研究</NavLink>
            <NavLink to="/backtest" className={linkCls}>回测</NavLink>
            <NavLink to="/live" className={linkCls}>实盘</NavLink>
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
        <Suspense fallback={<div className="p-8 text-sm text-slate-400">加载中…</div>}>
          <Routes>
            <Route path="/" element={<Navigate to="/dashboard" replace />} />
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/research" element={<Research />} />
            <Route path="/backtest" element={<Chat />} />
            <Route path="/chat" element={<Chat />} />
            <Route path="/live" element={<Live />} />
            <Route path="/risk" element={<Risk />} />
            <Route path="/settings" element={<Settings />} />
          </Routes>
        </Suspense>
      </main>

      <footer className="border-t border-white/5 py-4 text-center text-xs text-slate-500">
        证据优先 · 可追溯 · 不构成投资建议 — hero-quant
      </footer>
    </div>
  )
}

export default function App() {
  return <Shell />
}
