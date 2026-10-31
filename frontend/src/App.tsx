/**
 * 应用装配（App）
 * - Shell 承载全局布局：顶部导航 + 路由内容区 + 底部声明
 * - 路由结构：/ → /dashboard；/research 研究；/backtest 与 /chat 复用 Chat（回测对话）；/live 实盘；/risk 风控；/settings 设置；
 *   /compliance 合规与免责（PR-172：只读静态页，不进 RouteGuard，任何角色含无令牌均可读）
 * - 页面均懒加载，Suspense 提供加载态，首屏轻量
 */
import { Suspense, lazy, useEffect, useState } from "react"
import { Link, NavLink, Route, Routes, Navigate } from "react-router-dom"
import DegradedBanner from "./components/DegradedBanner"
import RouteGuard, { useTokenInfo } from "./components/RouteGuard"
import { NO_DIST_503_HINT, isServiceUnavailable } from "./config/degraded"
import { APP_VERSION } from "./config/version"
import { getToken, hasRole, highestRole, roleLabel, type Role } from "./config/auth"

const Dashboard = lazy(() => import("./pages/Dashboard"))
const Research = lazy(() => import("./pages/Research"))
const Chat = lazy(() => import("./pages/Chat"))
const Live = lazy(() => import("./pages/Live"))
const Risk = lazy(() => import("./pages/Risk"))
const Monitor = lazy(() => import("./pages/Monitor"))
const Settings = lazy(() => import("./pages/Settings"))
const Compliance = lazy(() => import("./pages/Compliance"))

/**
 * 导航项与所需角色（PR-103）：角色口径对齐后端端点 —— `/v1/query*` = analyst，
 * `/v1/backtest|sessions*` = viewer。设置页不在此表：换令牌入口必须永远可达。
 */
const NAV_ITEMS: { to: string; label: string; required: Role }[] = [
  { to: "/dashboard", label: "看板", required: "viewer" },
  { to: "/research", label: "研究", required: "viewer" },
  { to: "/backtest", label: "回测", required: "analyst" },
  { to: "/live", label: "实盘", required: "viewer" },
  { to: "/monitor", label: "监控", required: "viewer" },
  { to: "/risk", label: "风控", required: "viewer" },
]

/** 锁形提示：不隐藏链接（避免用户找不到页面），只表达「按令牌声明预期权限不足，是否拒绝由后端定」 */
function LockHint() {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className="h-3 w-3 shrink-0" fill="none" stroke="currentColor" strokeWidth={2}>
      <rect x="5" y="11" width="14" height="9" rx="2" />
      <path d="M8 11V8a4 4 0 0 1 8 0v3" />
    </svg>
  )
}

/** 顶栏角色 chip：数据来自令牌声明，文案必须带「未验签」 */
function RoleChip() {
  const info = useTokenInfo()
  const token = getToken()
  const top = highestRole(info)
  const text = !token ? "未提供令牌" : top ? `角色：${top}（未验签）` : "角色未声明"
  const title = !token
    ? "本机未配置令牌：若后端已开启鉴权，请求将被 401/403"
    : top
      ? `令牌声明角色：${info.roles.map(r => `${r}（${roleLabel(r)}）`).join("、")}；未验签，实际权限以服务端裁决为准`
      : "令牌未声明任何已知角色；权限以服务端裁决为准"
  const tone = !token
    ? "border-white/10 bg-white/5 text-slate-400"
    : top
      ? "border-emerald-400/20 bg-emerald-400/10 text-emerald-200"
      : "border-amber-500/25 bg-amber-500/10 text-amber-200"
  return (
    <span data-testid="role-chip" title={title} className={`rounded-full border px-2.5 py-1 text-[11px] ${tone}`}>
      {text}
    </span>
  )
}

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

  // 导航锁定：仅在「有令牌 + 令牌明确声明了角色」时判定；advisory 状态（无令牌/无角色）不动导航。
  // 链接仍可点击 → 进入页面看到 RouteGuard 的 403 解释面板（不发请求），故用 aria-disabled 表达预期而非拦截点击。
  const info = useTokenInfo()
  const navLockable = getToken() !== null && info.roles.length > 0

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
            <span className="ml-2 hidden items-center gap-1.5 rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-xs text-slate-300 md:inline-flex"><span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse shadow-[0_0_6px_rgba(52,211,153,0.8)]" />{APP_VERSION} · demo-ready</span>
          </div>

          <nav aria-label="Primary" className="flex max-w-[60vw] items-center gap-1 overflow-x-auto rounded-2xl border border-white/10 bg-white/[0.04] p-1 backdrop-blur scrollbar-none md:max-w-none">
            {NAV_ITEMS.map(item => {
              const locked = navLockable && !hasRole(info, item.required)
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
                  data-locked={locked ? "true" : undefined}
                  aria-disabled={locked ? "true" : undefined}
                  title={
                    locked
                      ? `需要 ${item.required} 及以上；当前令牌声明：${info.roles.join(", ")}（未验签，以服务端为准）`
                      : undefined
                  }
                  className={({ isActive }) => linkCls({ isActive }) + (locked ? " opacity-60" : "")}
                >
                  <span className="inline-flex items-center gap-1">
                    {item.label}
                    {locked ? <LockHint /> : null}
                  </span>
                </NavLink>
              )
            })}
          </nav>

          <div className="hidden items-center gap-2 md:flex shrink-0">
            <RoleChip />
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
            {/* 路由守卫：前端只做 UX 提示，真正的 401/403 由后端返回（见 RouteGuard.tsx 头注释） */}
            <Route path="/dashboard" element={<RouteGuard required="viewer" title="看板"><Dashboard /></RouteGuard>} />
            <Route path="/research" element={<RouteGuard required="viewer" title="研究"><Research /></RouteGuard>} />
            {/* 回测与对话复用同一 Chat 组件，/backtest 为历史入口兼容；两者都打 /v1/query*（analyst） */}
            <Route path="/backtest" element={<RouteGuard required="analyst" title="回测"><Chat /></RouteGuard>} />
            <Route path="/chat" element={<RouteGuard required="analyst" title="对话"><Chat /></RouteGuard>} />
            <Route path="/live" element={<RouteGuard required="viewer" title="实盘"><Live /></RouteGuard>} />
            <Route path="/monitor" element={<RouteGuard required="viewer" title="监控"><Monitor /></RouteGuard>} />
            <Route path="/risk" element={<RouteGuard required="viewer" title="风控"><Risk /></RouteGuard>} />
            {/* 设置页不设角色闸：换令牌入口必须永远可达，否则低权令牌无法自救 */}
            <Route path="/settings" element={<Settings />} />
            {/* 合规页同样不设角色闸：免责/数据来源/令牌口径必须在任何角色下可读，
                由页脚与设置页链接进入，故不占用主导航（见 NAV_ITEMS） */}
            <Route path="/compliance" element={<Compliance />} />
          </Routes>
        </Suspense>
      </main>

      <footer className="border-t border-white/5 py-4 text-center text-xs text-slate-500">
        <span>证据优先 · PIT 已校验 · Kepler — 不构成投资建议</span>
        <span className="mx-2 text-slate-600" aria-hidden="true">·</span>
        {/* 合规入口放页脚而非主导航：不挤占已有 6 项导航，且任何角色都可点（无 RouteGuard） */}
        <Link
          to="/compliance"
          data-testid="footer-compliance-link"
          className="underline decoration-dotted underline-offset-2 hover:text-mist transition"
        >
          合规与免责
        </Link>
      </footer>
    </div>
  )
}

export default function App() {
  return <Shell />
}
