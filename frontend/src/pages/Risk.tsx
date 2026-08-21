export default function Risk() {
  return (
    <div className="mx-auto max-w-7xl px-6 py-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-xl font-semibold text-mist">Risk · 风控</h1>
          <p className="mt-1 text-sm text-slate-400">敞口 · 熔断 · 归因 · 证据链 · ShadowAccount</p>
        </div>
        <span className="rounded-full border border-emerald-400/20 bg-emerald-400/10 px-3 py-1 text-xs font-medium text-emerald-300">风控正常 · CLOSED</span>
      </div>

      <div className="mt-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        {[
          { k: "总敞口", v: "62%", sub: "杠杆 1.2x" },
          { k: "单票上限", v: "20%", sub: "600519 18%" },
          { k: "日内熔断", v: "未触发", sub: "阈值 80%" },
          { k: "拒单率", v: "0.3%", sub: "PIT/证据链" },
        ].map((c) => (
          <div key={c.k} className="rounded-2xl border border-white/10 bg-white/[0.04] p-4 backdrop-blur">
            <div className="text-[11px] tracking-[0.14em] text-slate-400">{c.k}</div>
            <div className="mt-1 font-display text-xl font-semibold text-mist">{c.v}</div>
            <div className="font-mono text-[11px] text-slate-500">{c.sub}</div>
          </div>
        ))}
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <div className="rounded-2xl border border-white/10 bg-ink-800/60 p-4 backdrop-blur">
          <h2 className="text-sm font-semibold text-mist">风控规则 · 3-5条</h2>
          <div className="mt-3 space-y-2 text-xs">
            <div className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2">
              <span className="text-slate-400">PIT 校验</span>
              <span className="text-emerald-300">w ≤ p 否则 ValidationError</span>
            </div>
            <div className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2">
              <span className="text-slate-400">cross_source 1%</span>
              <span className="text-emerald-300">首bar偏差&gt;1% 阻断</span>
            </div>
            <div className="flex justify-between rounded-xl bg-amber-500/10 border border-amber-500/20 px-3 py-2">
              <span className="text-amber-300">熔断双桶</span>
              <span className="font-mono text-amber-100">Circuit 50% / OTel 80%</span>
            </div>
            <div className="flex justify-between rounded-xl bg-ink-900/60 border border-white/5 px-3 py-2">
              <span className="text-slate-400">Grounding</span>
              <span className="text-mist">证据链未命中阻断</span>
            </div>
          </div>
        </div>

        <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
          <h2 className="text-sm font-semibold text-mist">归因 · 5类 coverage&gt;0</h2>
          <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
            {[
              { k: "择时", v: "+1.2%" },
              { k: "选股", v: "+0.8%" },
              { k: "风控", v: "-0.1%" },
              { k: "成本", v: "-0.4%" },
              { k: "其他", v: "+0.2%" },
            ].map((x) => (
              <div key={x.k} className="rounded-xl border border-white/5 bg-white/[0.03] p-3 text-center">
                <div className="text-slate-400">{x.k}</div>
                <div className="mt-1 font-semibold text-mist">{x.v}</div>
              </div>
            ))}
          </div>
          <p className="mt-3 text-xs leading-5 text-slate-500">归因5类均有覆盖，coverage 100%；ShadowAccount 2.0 对账日跑。</p>
        </div>
      </div>

      <div className="mt-4 rounded-2xl border border-amber-400/20 bg-amber-400/10 p-4">
        <div className="text-xs font-semibold tracking-widest text-amber-300">审计</div>
        <p className="mt-2 text-sm leading-6 text-amber-100/90">
          所有风控决策写入 ledger.verify() · 证据链可追溯；超阈值自动走 compensate 分支并记录 trace。
        </p>
      </div>
    </div>
  )
}
