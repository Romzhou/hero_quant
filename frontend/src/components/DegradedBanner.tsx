/**
 * 降级横幅（E线）
 * - 职责：对 `degraded:true` 响应（含 D 线落地前的文本兜底判定）的统一横幅提示。
 * - 用法：`<DegradedBanner reason={reason} source="Chat SSE" />`，`reason` 为空时用兜底文案。
 */
import { DEGRADED_FALLBACK_REASON } from "../config/degraded"

export default function DegradedBanner({
  reason,
  source,
}: {
  reason?: string | null
  source?: string
}) {
  const text = reason && reason.trim() ? reason : DEGRADED_FALLBACK_REASON
  return (
    <div
      role="alert"
      data-testid="degraded-banner"
      className="flex flex-wrap items-center gap-2 rounded-xl border border-amber-400/30 bg-amber-400/10 px-4 py-2.5 text-sm text-amber-200"
    >
      <span className="h-2 w-2 shrink-0 rounded-full bg-amber-400 animate-pulse" aria-hidden />
      <span className="font-semibold">演示 / 降级模式</span>
      <span className="flex-1 leading-5">{text}</span>
      {source ? (
        <span className="rounded-full bg-white/10 px-2 py-0.5 font-mono text-[11px] text-amber-100/80">
          {source}
        </span>
      ) : null}
    </div>
  )
}
