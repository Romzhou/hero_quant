/**
 * 降级语义消费层（E线）
 * - 职责：统一消费 D 线约定的降级语义 —— 响应体 `degraded:true + reason`、`/ready` degraded 时 503。
 * - 现状（只读观测，不改 server.py）：`src/hero_quant/api/server.py:1091-1401` 的 `_FakeLLM`
 *   目前仅在文本中写“演示模式”，尚未返回 `degraded` 字段；`/ready:930-971` 已有
 *   `status: degraded + 503`。因此本模块做兼容判定：
 *   显式 `degraded:true` 优先；文本命中演示标记时同样视为降级（fallback），
 *   待 D 线落地 `degraded:true + reason` 后本模块无需改动即可直显后端 reason。
 * - 依赖D线：`_FakeLLM` / `emulated(server.py:229-235)` 响应体置 `degraded:true + reason`。
 */

/** 显式降级载荷判定：`{ degraded: true, ... }` 或 `{ type: "degraded", ... }` */
export function isDegradedPayload(j: unknown): boolean {
  if (!j || typeof j !== "object" || Array.isArray(j)) return false
  const o = j as Record<string, unknown>
  return o.degraded === true || o.type === "degraded"
}

/** 提取降级原因：`reason / msg / message`，缺失时返回 null（调用方用兜底文案） */
export function extractDegradedReason(j: unknown): string | null {
  if (!j || typeof j !== "object" || Array.isArray(j)) return null
  const o = j as Record<string, unknown>
  for (const k of ["reason", "msg", "message"]) {
    const v = o[k]
    if (typeof v === "string" && v.trim()) return v.trim()
  }
  return null
}

/** 合成演示文本标记（D 线落地前的兼容 fallback 判定依据） */
export const DEGRADED_TEXT_MARKERS = ["演示模式", "合成占位", "synthetic", "isMock"] as const

/** 文本兜底判定：delta 文本命中演示标记即视为降级（D 线未返回 degraded 字段时的兼容路径） */
export function detectDegradedFromText(t: unknown): string | null {
  if (typeof t !== "string" || !t) return null
  const hit = (DEGRADED_TEXT_MARKERS as readonly string[]).some(m => t.includes(m))
  if (!hit) return null
  return DEGRADED_FALLBACK_REASON
}

/** 无显式 reason 时的兜底横幅文案（含对 D 线的依赖声明） */
export const DEGRADED_FALLBACK_REASON =
  "合成演示数据（未配置 HERO_API_KEY），结论仅供演示，不构成投资建议。依赖D线：待后端返回 degraded:true+reason 后此横幅将直显后端原因。"

/** 503 判定：无 dist / 服务降级时后端约定的状态码（D 线 `/ready` degraded → 503，P0-3 无 dist → 503） */
export function isServiceUnavailable(status: unknown): boolean {
  return status === 503
}

/** 无 dist / 服务不可用时的前端提示文案（仅消费 D 线暴露的 503 语义，不改 server.py） */
export const NO_DIST_503_HINT =
  "后端返回 503（服务不可用 / 前端构建缺失）：请确认已执行 npm run build 生成 dist；若后端 503 语义未就绪请联系 D 线。当前为兼容展示，数据可能为占位。"
