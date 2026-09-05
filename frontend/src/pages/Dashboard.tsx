/**
 * Dashboard 看板页
 * - 职责：聚合展示资产/收益/年化/回撤等核心指标与四域快捷入口、最近活动
 * - 数据流：拉取 API_METRICS 真实指标，失败回退静态占位并通过 isMock 区分；骨架屏过渡
 * - 演示入口：顶部琥珀渐变 CTA 一键演示，写入 chat store 并通过 router state 跳转 /backtest
 */
import { useEffect, useState } from "react"
import { useNavigate, Link } from "react-router-dom"
import { useChatStore } from "../store/chat"
import { API_METRICS, ROUTES } from "../config/api"

type Metrics = { annual_return?: number; sharpe?: number; max_drawdown?: number; turnover?: number; total_equity?: number }

// 占位回退值：字段必填（Required），调用处不再需要 `!` 非空断言，避免缺 key 时运行时抛错
export const FALLBACK: Required<Omit<Metrics, "total_equity">> = { annual_return: 0.184, sharpe: 1.62, max_drawdown: -0.032, turnover: 0.42 }

// 兼容既有导入：API_METRICS 统一由 src/config/api 导出，此处 re-export 保持导入路径不变
export { API_METRICS }

// 抽取硬编码：演示查询常量，Hero 文案与 handleDemo 共用，避免文案/逻辑漂移
export const DEMO_QUERY = "回测 600519.SH 近一月等权"

function isFiniteNumber(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v)
}
function fmtPct(v: unknown, fallback: number): string {
  return isFiniteNumber(v) ? `${(v * 100).toFixed(1)}%` : `${(fallback * 100).toFixed(1)}%`
}
function fmtFixed(v: unknown, fallback: number, digits = 2): string {
  return isFiniteNumber(v) ? v.toFixed(digits) : fallback.toFixed(digits)
}

// 抽取重复 cast：统一数字选取，处理 total_equity/totalEquity 别名
function pickNumber(obj: Record<string, unknown>, key: string, fallback?: number): number | undefined {
  const v: unknown = obj[key]
  return isFiniteNumber(v) ? v : fallback
}

// 徽标语义（fail-closed：loading > mock > ready）：if/else 替代嵌套三元，满足 no-nested-ternary
function getBadge(loading: boolean, isMock: boolean): { text: string; cls: string } {
  if (loading) return { text: "加载中", cls: "border-slate-400/20 bg-slate-400/10 text-slate-300" }
  if (isMock) return { text: "占位数据", cls: "border-amber-400/20 bg-amber-400/10 text-amber-300" }
  return { text: "数据就绪", cls: "border-emerald-400/20 bg-emerald-400/10 text-emerald-300" }
}

export default function Dashboard() {
  const navigate = useNavigate()
  // isMock 区分：初始为占位态，成功后置 false，失败保持 true，避免 FALLBACK 被误读为真实数据
  const [metrics, setMetrics] = useState<Metrics | null>(null)
  const [isMock, setIsMock] = useState(true)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    const { signal } = controller
    async function load() {
      setError(null)
      setLoading(true)
      try {
        const r = await fetch(API_METRICS, { cache: "no-store", signal })
        if (!r.ok) throw new Error(String(r.status))
        const j: unknown = await r.json()
        if (!signal.aborted && typeof j === "object" && j !== null && !Array.isArray(j)) {
          const obj: Record<string, unknown> = j as Record<string, unknown>
          // 统⼀通过 pickNumber 选取，避免逐字段重复 cast 带来的扩展错误
          // 有效载荷门槛：零真实数字字段（{} / 全非法）不得标 ready，走 error + 占位回退
          const annual_return = pickNumber(obj, "annual_return")
          const sharpe = pickNumber(obj, "sharpe")
          const max_drawdown = pickNumber(obj, "max_drawdown")
          const turnover = pickNumber(obj, "turnover")
          const total_equity = pickNumber(obj, "total_equity", pickNumber(obj, "totalEquity"))
          const hasRealField = [annual_return, sharpe, max_drawdown, turnover, total_equity].some(isFiniteNumber)
          if (!hasRealField) throw new Error("invalid metrics payload")
          const parsed: Metrics = {
            annual_return: annual_return ?? FALLBACK.annual_return,
            sharpe: sharpe ?? FALLBACK.sharpe,
            max_drawdown: max_drawdown ?? FALLBACK.max_drawdown,
            turnover: turnover ?? FALLBACK.turnover,
            total_equity,
          }
          setMetrics(parsed)
          setIsMock(false)
        }
      } catch (e) {
        if (signal.aborted) return
        if (e instanceof DOMException && e.name === "AbortError") return
        const msg = e instanceof Error ? e.message : String(e)
        console.error("[Dashboard] metrics fetch failed:", msg)
        setError(msg)
        // 失败回退占位，但保持 isMock=true 以便徽标与卡片可区分
        setMetrics(FALLBACK)
        setIsMock(true)
      } finally {
        if (!signal.aborted) setLoading(false)
      }
    }
    load()
    return () => { controller.abort() }
  }, [reloadKey])

  const handleDemo = () => {
    // 脆弱点修复：同时写入 store 并通过 router state 传递，Chat 侧以 state.prefill 为可信来源，store 为增强
    useChatStore.getState().setInput(DEMO_QUERY)
    navigate(ROUTES.BACKTEST, { state: { prefill: DEMO_QUERY } })
  }

  // 展示用有效指标：未加载到真实数据时使用 FALLBACK 占位，但 isMock 徽标会明确标识
  const displayMetrics: Metrics = metrics ?? FALLBACK
  const totalEquityDisplay = loading ? "…" : (isFiniteNumber(displayMetrics.total_equity) ? `¥ ${displayMetrics.total_equity.toLocaleString("zh-CN")}` : "—")
  // 移除死字段 accent：原 Card.accent 未被渲染消费，保留会误导后续样式扩展
  type Card = { k: string; v: string; sub: string; isEquity?: boolean }
  const cards: Card[] = [
    { k: "总资产", v: totalEquityDisplay, sub: "含现金", isEquity: true },
    { k: "年化", v: loading ? "…" : fmtPct(displayMetrics.annual_return, FALLBACK.annual_return), sub: `sharpe ${fmtFixed(displayMetrics.sharpe, FALLBACK.sharpe)}` },
    { k: "最大回撤", v: loading ? "…" : fmtPct(displayMetrics.max_drawdown, FALLBACK.max_drawdown), sub: "近30日" },
    { k: "换手率", v: loading ? "…" : fmtFixed(displayMetrics.turnover, FALLBACK.turnover, 2), sub: "turnover" },
  ]

  // 徽标 fail-closed 语义：加载中/占位数据/数据就绪 三态，避免无条件“数据就绪”误导
  const badge = getBadge(loading, isMock)

  return (
    <div className="mx-auto max-w-7xl px-6 py-6">
      {/* 一键演示 Hero */}
      <div className="relative overflow-hidden rounded-2xl border border-amber-400/20 bg-gradient-to-br from-amber-500 via-amber-500 to-orange-500 p-[1px]">
        <div className="rounded-[15px] bg-gradient-to-br from-amber-500 to-orange-500 px-5 py-5 md:px-6 md:py-6">
          <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
            <div className="flex-1">
              <div className="inline-flex items-center gap-2 rounded-full bg-white/20 px-3 py-1 text-xs font-semibold text-white backdrop-blur">
                <span className="h-1.5 w-1.5 rounded-full bg-white animate-pulse" /> DEMO READY · 30秒跑通
              </div>
              <h2 className="mt-2 font-display text-lg font-bold leading-tight text-white md:text-xl">一键演示：从自然语言到真回测</h2>
              <p className="mt-1 text-sm leading-5 text-white/85">预填 <span className="rounded bg-white/20 px-1.5 py-0.5 font-mono text-xs">{DEMO_QUERY}</span> · 点击后跳转对话页，SSE 流式返回 tool 轨迹与净值</p>
              <p className="mt-1 hidden text-xs text-white/70 md:block">真实链路：registry → tencent/yahoo → engine → positions.csv / metrics.json</p>
            </div>
            <div className="flex shrink-0 flex-col gap-2">
              <button onClick={handleDemo} className="rounded-xl bg-white px-6 py-3 text-sm font-bold text-amber-700 shadow-lg hover:bg-white/95 transition flex items-center justify-center gap-1.5">
                ▶ 一键演示
              </button>
              <span className="text-center text-[11px] text-white/70">自动填入并跳转 /backtest</span>
            </div>
          </div>
        </div>
      </div>

      <div className="mt-6 flex items-center justify-between">
        <div>
          <h1 className="font-display text-xl font-semibold text-mist">Dashboard · 总览</h1>
          <p className="mt-1 text-sm text-slate-400">今日概览 · 资产 · 收益 · 风控 · 活动</p>
        </div>
        <span className={"rounded-full border px-3 py-1 text-xs font-medium " + badge.cls}>{badge.text}</span>
      </div>

      {error && !loading && (
        <div
          role="alert"
          className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3"
        >
          <div className="flex items-center gap-2 text-sm text-amber-200">
            <span className="h-2 w-2 rounded-full bg-amber-400 animate-pulse" aria-hidden />
            数据获取失败，显示为占位数据{error ? `（${error}）` : ""}
          </div>
          <button
            type="button"
            onClick={() => setReloadKey(k => k + 1)}
            className="rounded-full border border-amber-500/30 bg-amber-500/20 px-3 py-1 text-xs font-medium text-amber-100 hover:bg-amber-500/30"
          >
            重试
          </button>
        </div>
      )}

      {/* 指标卡：骨架 + 真实指标 */}
      <div className="mt-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        {loading ? (
          <>
            {[0,1,2,3].map(i => (
              <div key={i} className="rounded-2xl border border-white/10 bg-white/[0.04] p-4 backdrop-blur animate-pulse">
                <div className="h-3 w-12 rounded bg-white/10" />
                <div className="mt-3 h-6 w-20 rounded bg-white/10" />
                <div className="mt-2 h-3 w-16 rounded bg-white/5" />
              </div>
            ))}
          </>
        ) : (
          cards.map((c, i) => (
            <div key={c.k} style={{ animationDelay: `${i * 80}ms` }} className="group rounded-2xl border border-white/10 bg-white/[0.04] p-4 backdrop-blur transition hover:bg-white/[0.06] hover:border-white/15 hover:shadow-lg hover:-translate-y-0.5 animate-[fadeIn_0.5s_ease_both]">
              <div className="text-[11px] tracking-[0.14em] text-slate-400">{c.k}</div>
              <div className="mt-1 font-display text-xl font-semibold text-mist group-hover:text-white transition">
                {c.isEquity && !isFiniteNumber(displayMetrics.total_equity) ? "—" : c.v}
              </div>
              <div className="font-mono text-[11px] text-slate-500">{c.sub}</div>
            </div>
          ))
        )}
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-3">
        <div className="rounded-2xl border border-white/10 bg-ink-800/60 p-4 backdrop-blur">
          <h2 className="text-sm font-semibold text-mist">快捷入口</h2>
          <div className="mt-3 flex flex-wrap gap-2">
            <Link to={ROUTES.RESEARCH} className="rounded-xl bg-amber-500 px-3 py-2 text-xs font-semibold text-ink-900 hover:bg-amber-400 transition">去研究</Link>
            <Link to={ROUTES.BACKTEST} className="rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-xs text-mist hover:bg-white/10 transition">去回测</Link>
            <Link to={ROUTES.LIVE} className="rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-xs text-mist hover:bg-white/10 transition">实盘监控</Link>
          </div>
          <p className="mt-3 text-xs leading-5 text-slate-500">聚合 研究/回测/实盘/风控 四域状态；深墨+琥珀视觉统一。</p>
        </div>
        <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4 lg:col-span-2">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-mist">活动</h2>
            <span className="text-[11px] text-slate-500">点击直达</span>
          </div>
          <ul className="mt-3 space-y-2 text-xs">
            <li>
              <Link to={ROUTES.RESEARCH} className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2 hover:border-amber-500/20 hover:bg-amber-500/5 transition">
                <span className="text-slate-400">回测完成</span><span className="text-mist">600519.SH 等权 · {fmtPct(displayMetrics.annual_return, FALLBACK.annual_return)} 年化 → 研究 ↗</span>
              </Link>
            </li>
            <li>
              <Link to={ROUTES.RISK} className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2 hover:border-white/10 transition">
                <span className="text-slate-400">风控</span><span className="text-emerald-300">PIT 已校验 · 未阻断 → 风控</span>
              </Link>
            </li>
            <li>
              <Link to={ROUTES.LIVE} className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2 hover:border-white/10 transition">
                <span className="text-slate-400">实盘</span><span className="text-slate-300">events.jsonl 实时流 → Live</span>
              </Link>
            </li>
          </ul>
        </div>
      </div>
    </div>
  )
}
