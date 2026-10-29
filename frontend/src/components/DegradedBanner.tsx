/**
 * 降级/失败横幅（E线）
 * - 两种语义共用一条横幅，但颜色与标题必须区分：
 *   · degraded（琥珀）= 结果可用但已降级（演示数据、LLM failover、checkpoint 回退）
 *   · failure（红）= 请求被拒绝 / 服务不可用（401 / 403 / 429 / kill switch / 5xx）
 * - 用法：`<DegradedBanner reason={...} />` 或 `<DegradedBanner failure={describeHttpFailure(...)} />`
 */
import { DEGRADED_FALLBACK_REASON, FAILURE_LABEL, type FailureKind, type HttpFailure } from "../config/degraded"

type Tone = { wrap: string; dot: string; badge: string }

const DEGRADED_TONE: Tone = {
  wrap: "border-amber-400/30 bg-amber-400/10 text-amber-200",
  dot: "bg-amber-400",
  badge: "bg-white/10 text-amber-100/80",
}

const FAILURE_TONE: Tone = {
  wrap: "border-rose-500/40 bg-rose-500/10 text-rose-200",
  dot: "bg-rose-400",
  badge: "bg-white/10 text-rose-100/80",
}

export default function DegradedBanner({
  reason,
  source,
  failure,
}: {
  reason?: string | null
  source?: string
  /** 系统级失败；优先于 reason，传入即按失败样式渲染 */
  failure?: HttpFailure | null
}) {
  const isFailure = !!failure
  const tone = isFailure ? FAILURE_TONE : DEGRADED_TONE
  const title = isFailure ? FAILURE_LABEL[(failure as HttpFailure).kind as FailureKind] : "演示 / 降级模式"
  const text = isFailure
    ? (failure as HttpFailure).message
    : reason && reason.trim()
      ? reason
      : DEGRADED_FALLBACK_REASON
  const retryAfter = isFailure ? (failure as HttpFailure).retryAfterSeconds : undefined

  return (
    <div
      role="alert"
      data-testid={isFailure ? "failure-banner" : "degraded-banner"}
      data-failure-kind={isFailure ? (failure as HttpFailure).kind : undefined}
      className={`flex flex-wrap items-center gap-2 rounded-xl border px-4 py-2.5 text-sm ${tone.wrap}`}
    >
      <span className={`h-2 w-2 shrink-0 rounded-full animate-pulse ${tone.dot}`} aria-hidden />
      <span className="font-semibold">{title}</span>
      <span className="flex-1 leading-5">{text}</span>
      {retryAfter != null ? (
        <span className={`rounded-full px-2 py-0.5 font-mono text-[11px] ${tone.badge}`}>{retryAfter}s 后重试</span>
      ) : null}
      {source ? (
        <span className={`rounded-full px-2 py-0.5 font-mono text-[11px] ${tone.badge}`}>{source}</span>
      ) : null}
    </div>
  )
}
