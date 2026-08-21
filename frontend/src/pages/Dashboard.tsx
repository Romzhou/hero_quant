export default function Dashboard() {
  return (
    <div className="mx-auto max-w-7xl px-6 py-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-display text-xl font-semibold text-mist">Dashboard · 总览</h1>
          <p className="mt-1 text-sm text-slate-400">今日概览 · 资产 · 收益 · 风控 · 活动</p>
        </div>
        <span className="rounded-full border border-emerald-400/20 bg-emerald-400/10 px-3 py-1 text-xs font-medium text-emerald-300">数据就绪</span>
      </div>

      <div className="mt-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        {[
          { k: "总资产", v: "¥ 1,284,520", sub: "含现金" },
          { k: "今日收益", v: "+0.82%", sub: "+¥10,530" },
          { k: "年化", v: "18.4%", sub: "sharpe 1.62" },
          { k: "最大回撤", v: "-3.2%", sub: "近30日" },
        ].map((c, i) => (
          <div key={c.k} style={{ animationDelay: `${i * 80}ms` }} className="group rounded-2xl border border-white/10 bg-white/[0.04] p-4 backdrop-blur transition hover:bg-white/[0.06] hover:border-white/15 animate-[fadeIn_0.5s_ease_both]">
            <div className="text-[11px] tracking-[0.14em] text-slate-400">{c.k}</div>
            <div className="mt-1 font-display text-xl font-semibold text-mist group-hover:text-white transition">{c.v}</div>
            <div className="font-mono text-[11px] text-slate-500">{c.sub}</div>
          </div>
        ))}
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-3">
        <div className="rounded-2xl border border-white/10 bg-ink-800/60 p-4 backdrop-blur">
          <h2 className="text-sm font-semibold text-mist">快捷入口</h2>
          <div className="mt-3 flex flex-wrap gap-2">
            <a href="/research" className="rounded-xl bg-amber-500 px-3 py-2 text-xs font-semibold text-ink-900">去研究</a>
            <a href="/backtest" className="rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-xs text-mist">去回测</a>
            <a href="/live" className="rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-xs text-mist">实盘监控</a>
          </div>
          <p className="mt-3 text-xs leading-5 text-slate-500">聚合 研究/回测/实盘/风控 四域状态；深墨+琥珀视觉统一。</p>
        </div>
        <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4 lg:col-span-2">
          <h2 className="text-sm font-semibold text-mist">活动</h2>
          <ul className="mt-3 space-y-2 text-xs">
            <li className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2"><span className="text-slate-400">回测完成</span><span className="text-mist">600519.SH 等权 · 18.4% 年化</span></li>
            <li className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2"><span className="text-slate-400">风控</span><span className="text-emerald-300">PIT 已校验 · 未阻断</span></li>
            <li className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2"><span className="text-slate-400">实盘</span><span className="text-slate-300">events.jsonl offset 128 · CLOSED</span></li>
          </ul>
        </div>
      </div>
    </div>
  )
}
