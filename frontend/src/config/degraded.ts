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

// ---------------------------------------------------------------------------
// 失败语义（t14 批次1）：D 线接线后 401/403/429/503 都是真实拒绝，
// 页面不得再把它们吞成「mock / 演示数据」——那等于把熔断和未登录伪装成正常结果。
// ---------------------------------------------------------------------------

/** 系统级失败分类；`kind` 只由状态码 + 后端机器码推导，不靠文案猜 */
export type FailureKind = "unauthorized" | "forbidden" | "quota" | "killswitch" | "unavailable"

export interface HttpFailure {
  kind: FailureKind
  message: string
  /** 429 的 Retry-After（秒），有值时前端可提示重试时机 */
  retryAfterSeconds?: number
}

/** 各类失败的横幅标题（与降级横幅区分：失败用红，降级用琥珀） */
export const FAILURE_LABEL: Record<FailureKind, string> = {
  unauthorized: "需要登录 / 登录已失效",
  forbidden: "权限不足",
  quota: "请求配额已用尽",
  killswitch: "服务已熔断",
  unavailable: "服务不可用",
}

function _asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function _str(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v.trim() : null
}

/** FastAPI 两种错误体都兼容：`{"detail": "...", "code": "..."}` 与 `{"detail": {"reason": "...", "kill_reason": "..."}}` */
function _bodyDetail(body: unknown): { text: string | null; reason: string | null; killReason: string | null; code: string | null } {
  const o = _asRecord(body)
  if (!o) return { text: null, reason: null, killReason: null, code: null }
  const d = o.detail
  const dr = _asRecord(d)
  return {
    text: _str(dr ? null : d) ?? _str(o.message) ?? _str(o.msg),
    reason: dr ? _str(dr.reason) : null,
    killReason: dr ? _str(dr.kill_reason) ?? _str(dr.detail) : null,
    code: _str(o.code),
  }
}

/**
 * HTTP 失败 → 归一化失败语义；`null` 表示「不是系统级失败」（2xx、404 空态、其它 4xx 入参错误），
 * 调用方按自己的空态逻辑处理，不要弹失败横幅。
 */
export function describeHttpFailure(
  status: number,
  body: unknown,
  retryAfterHeader?: string | null,
): HttpFailure | null {
  if (status >= 200 && status < 400) return null
  const { text, reason, killReason, code } = _bodyDetail(body)

  if (status === 401) {
    return { kind: "unauthorized", message: text ?? code ?? "未通过身份认证，请配置访问令牌" }
  }
  if (status === 403) {
    return { kind: "forbidden", message: text ?? code ?? "当前角色无权访问该接口" }
  }
  if (status === 429) {
    const secs = Number.parseInt(retryAfterHeader ?? "", 10)
    return {
      kind: "quota",
      message: text ?? code ?? "触发限流/配额上限",
      ...(Number.isFinite(secs) ? { retryAfterSeconds: secs } : {}),
    }
  }
  if (status === 503) {
    // kill switch 与「后端不可用」都按 fail-closed 拒绝，必须单独成类，不能混成普通 503
    if (reason === "killswitch_active") {
      return { kind: "killswitch", message: killReason ?? "kill switch 已触发，请求被拒绝" }
    }
    if (reason === "killswitch_unavailable") {
      return { kind: "killswitch", message: killReason ?? "kill switch 后端不可用，按 fail-closed 拒绝请求" }
    }
    if (reason === "auth_required_for_sessions" || code === "missing_tenant") {
      return { kind: "unauthorized", message: text ?? "会话接口要求已认证租户" }
    }
    return { kind: "unavailable", message: text ?? reason ?? code ?? NO_DIST_503_HINT }
  }
  if (status >= 500) {
    return { kind: "unavailable", message: text ?? reason ?? code ?? `后端返回 HTTP ${status}` }
  }
  // 400/404/422 等：入参或资源问题，交给页面自身的空态/错误态
  return null
}

/**
 * 2xx 响应里的降级标记汇总（D 线已落地 `degraded:true + degraded_reason`）：
 * 优先后端 `degraded_reason`，并补译 `llm_mode:"failover"` / `checkpoint_mode:"emulated"` 两个机器态。
 */
export function extractDegradedFromResponse(j: unknown): string | null {
  const o = _asRecord(j)
  if (!o) return null
  const reasons: string[] = []
  const explicit = _str(o.degraded_reason) ?? extractDegradedReason(j)
  if (explicit) reasons.push(explicit)
  if (o.llm_mode === "failover") reasons.push("LLM 主模型不可用，已切换到备用 provider")
  if (o.checkpoint_mode === "emulated") reasons.push("checkpoint 未接 Postgres，已回退内存（进程重启即丢）")
  if (reasons.length) return reasons.join("；")
  return isDegradedPayload(j) ? DEGRADED_FALLBACK_REASON : null
}
