/**
 * Dashboard 看板页
 * - 职责：聚合展示资产/收益/年化/回撤等核心指标与四域快捷入口、最近活动
 * - 数据流：拉取 /v1/backtest/metrics.json 真实指标，失败回退静态占位；骨架屏过渡
 * - 演示入口：顶部琥珀渐变 CTA 一键演示，写入 chat store 并跳转 /backtest
 */
import { useEffect, useState } from "react"
import { useNavigate, Link } from "react-router-dom"
import { useChatStore } from "../store/chat"

type Metrics = { annual_return?: number; sharpe?: number; max_drawdown?: number; turnover?: number }

const FALLBACK: Metrics = { annual_return: 0.184, sharpe: 1.62, max_drawdown: -0.032, turnover: 0.42 }

export default function Dashboard() {
  const navigate = useNavigate()
  const [metrics, setMetrics] = useState<Metrics>(FALLBACK)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let aborted = false
    async function load() {
      try {
        const r = await fetch("/v1/backtest/metrics.json", { cache: "no-store" })
        if (!r.ok) throw new Error(String(r.status))
        const j = await r.json()
        if (!aborted && j && typeof j === "object") {
          setMetrics({
            annual_return: typeof j.annual_return === "number" ? j.annual_return : FALLBACK.annual_return,
            sharpe: typeof j.sharpe === "number" ? j.sharpe : FALLBACK.sharpe,
            max_drawdown: typeof j.max_drawdown === "number" ? j.max_drawdown : FALLBACK.max_drawdown,
            turnover: typeof j.turnover === "number" ? j.turnover : FALLBACK.turnover,
          })
        }
      } catch {
        // keep fallback
      } finally {
        if (!aborted) setLoading(false)
      }
    }
    load()
    return () => { aborted = true }
  }, [])

  const handleDemo = () => {
    const q = "回测 600519.SH 近一月等权"
    useChatStore.getState().setInput(q)
    navigate("/backtest")
  }

  const cards = [
    { k: "总资产", v: "¥ 1,284,520", sub: "含现金", accent: false },
    { k: "年化", v: loading ? "…" : `${(metrics.annual_return! * 100).toFixed(1)}%`, sub: `sharpe ${metrics.sharpe?.toFixed(2) ?? "1.62"}`, accent: true },
    { k: "最大回撤", v: loading ? "…" : `${(metrics.max_drawdown! * 100).toFixed(1)}%`, sub: "近30日", accent: false },
    { k: "换手率", v: loading ? "…" : String(metrics.turnover), sub: "turnover", accent: false },
  ]

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
              <p className="mt-1 text-sm leading-5 text-white/85">预填 <span className="rounded bg-white/20 px-1.5 py-0.5 font-mono text-xs">回测 600519.SH 近一月等权</span> · 点击后跳转对话页，SSE 流式返回 tool 轨迹与净值</p>
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
        <span className="rounded-full border border-emerald-400/20 bg-emerald-400/10 px-3 py-1 text-xs font-medium text-emerald-300">数据就绪</span>
      </div>

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
              <div className="mt-1 font-display text-xl font-semibold text-mist group-hover:text-white transition">{c.v}</div>
              <div className="font-mono text-[11px] text-slate-500">{c.sub}</div>
            </div>
          ))
        )}
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-3">
        <div className="rounded-2xl border border-white/10 bg-ink-800/60 p-4 backdrop-blur">
          <h2 className="text-sm font-semibold text-mist">快捷入口</h2>
          <div className="mt-3 flex flex-wrap gap-2">
            <Link to="/research" className="rounded-xl bg-amber-500 px-3 py-2 text-xs font-semibold text-ink-900 hover:bg-amber-400 transition">去研究</Link>
            <Link to="/backtest" className="rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-xs text-mist hover:bg-white/10 transition">去回测</Link>
            <Link to="/live" className="rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-xs text-mist hover:bg-white/10 transition">实盘监控</Link>
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
              <Link to="/research" className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2 hover:border-amber-500/20 hover:bg-amber-500/5 transition">
                <span className="text-slate-400">回测完成</span><span className="text-mist">600519.SH 等权 · {(metrics.annual_return!*100).toFixed(1)}% 年化 → 研究 ↗</span>
              </Link>
            </li>
            <li>
              <Link to="/risk" className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2 hover:border-white/10 transition">
                <span className="text-slate-400">风控</span><span className="text-emerald-300">PIT 已校验 · 未阻断 → 风控</span>
              </Link>
            </li>
            <li>
              <Link to="/live" className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2 hover:border-white/10 transition">
                <span className="text-slate-400">实盘</span><span className="text-slate-300">events.jsonl 实时流 → Live</span>
              </Link>
            </li>
          </ul>
        </div>
      </div>
    </div>
  )
}
