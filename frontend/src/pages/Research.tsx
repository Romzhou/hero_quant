import { useMemo } from "react"
import ReactECharts from "echarts-for-react"

export default function Research() {
  const option = useMemo(() => ({
    backgroundColor: "transparent",
    textStyle: { color: "#94A3B8" },
    grid: { left: 40, right: 16, top: 24, bottom: 28 },
    tooltip: { trigger: "axis" as const },
    xAxis: {
      type: "category" as const,
      data: ["08-12","08-13","08-14","08-15","08-16","08-19","08-20"],
      axisLine: { lineStyle: { color: "rgba(255,255,255,0.1)" } },
      axisLabel: { color: "#94A3B8" }
    },
    yAxis: {
      type: "value" as const,
      axisLine: { show: false },
      splitLine: { lineStyle: { color: "rgba(255,255,255,0.06)" } },
      axisLabel: { color: "#94A3B8" }
    },
    series: [
      {
        name: "600519 等权净值",
        type: "line" as const,
        smooth: true,
        symbol: "none",
        lineStyle: { width: 2, color: "#F59E0B" },
        areaStyle: { color: "rgba(245,158,11,0.15)" },
        data: [1.0, 1.01, 0.995, 1.02, 1.04, 1.03, 1.06]
      },
      {
        name: "沪深300",
        type: "line" as const,
        smooth: true,
        symbol: "none",
        lineStyle: { width: 1.5, color: "#60A5FA" },
        data: [1.0, 1.003, 0.998, 1.01, 1.015, 1.012, 1.02]
      }
    ],
    legend: {
      bottom: 0,
      textStyle: { color: "#CBD5E1", fontSize: 11 },
      data: ["600519 等权净值","沪深300"]
    }
  }), [])

  return (
    <div className="mx-auto max-w-6xl px-6 py-6">
      {/* header */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-xl font-semibold text-mist">投研 · 研究</h1>
          <p className="mt-1 text-sm text-slate-400">策略回测 · 因子分析 · 证据链（后续接 memory / trace）</p>
        </div>
        <div className="flex gap-2">
          <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-slate-300">PIT 校验已启用</span>
          <span className="rounded-full border border-amber-400/20 bg-amber-400/10 px-3 py-1 text-xs text-amber-300">数据单位：板手 / 股 已标注</span>
        </div>
      </div>

      {/* metrics */}
      <div className="mt-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        {[
          { k: "年化收益", v: "+18.4%", sub: "近 1 月" },
          { k: "夏普", v: "1.62", sub: "无风险 2%" },
          { k: "最大回撤", v: "-3.2%", sub: "2026-08-14" },
          { k: "换手率", v: "0.42", sub: "日均" }
        ].map(c => (
          <div key={c.k} className="rounded-2xl border border-white/10 bg-white/[0.04] p-4 backdrop-blur">
            <div className="text-xs tracking-widest text-slate-400">{c.k}</div>
            <div className="mt-1 font-display text-xl font-semibold text-mist">{c.v}</div>
            <div className="text-xs text-slate-500">{c.sub}</div>
          </div>
        ))}
      </div>

      {/* chart */}
      <div className="mt-6 rounded-2xl border border-white/10 bg-ink-800/60 p-4 backdrop-blur">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-mist">净值曲线（示例）</h2>
          <span className="text-xs text-slate-500">ECharts · 占位数据 · 后续接 /v1/backtest</span>
        </div>
        <ReactECharts option={option} style={{ height: 280 }} opts={{ renderer: "canvas" }} />
        <div className="mt-2 rounded-xl border border-white/5 bg-white/[0.03] px-3 py-2 text-xs leading-5 text-slate-400">
          校验规则：权重日期 ≤ 价格日期（PIT）、拒绝混币种聚合、拒绝非正价格。证据由 <span className="text-amber-300">GroundingLedger</span> 校验，未命中将阻断展示。
        </div>
      </div>

      {/* provenance */}
      <div className="mt-6 grid gap-4 md:grid-cols-3">
        <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
          <div className="text-xs font-semibold tracking-widest text-slate-400">数据溯源</div>
          <ul className="mt-2 space-y-1.5 text-sm text-slate-300">
            <li>• 600519.SH — tencent（板手）</li>
            <li>• AAPL.US — yahoo（股）</li>
            <li>• 备用：registry 自动 fallback</li>
          </ul>
        </div>
        <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
          <div className="text-xs font-semibold tracking-widest text-slate-400">量化指标</div>
          <ul className="mt-2 space-y-1.5 text-sm text-slate-300">
            <li>• sma / ema / rsi / bollinger</li>
            <li>• max_drawdown · sharpe</li>
            <li>• 纯 pandas，无重依赖</li>
          </ul>
        </div>
        <div className="rounded-2xl border border-amber-400/20 bg-amber-400/10 p-4">
          <div className="text-xs font-semibold tracking-widest text-amber-300">下一步</div>
          <p className="mt-2 text-sm leading-6 text-amber-100/90">接入真实回测引擎后，此页将展示 positions.csv 与可下载报告（markdown / PDF）。</p>
        </div>
      </div>
    </div>
  )
}
