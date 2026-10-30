/**
 * 前端身份入口（E线 t14 批次2）
 * - 只做三件事：存令牌、把令牌挂到请求头、把令牌里的声明解出来给人看。
 * - 不验签、不裁决权限：令牌是否合法、租户是否有效一律由后端（W1 auth 中间件）裁决，
 *   前端解析结果仅供展示，必须在 UI 上标注「来自令牌声明，未经服务端确认」。
 */

/** localStorage 键名；令牌只落本机浏览器，不写日志、不发往任何第三方 */
export const TOKEN_STORAGE_KEY = "hero-quant.token"

export function getToken(): string | null {
  try {
    const v = localStorage.getItem(TOKEN_STORAGE_KEY)
    return v && v.trim() ? v.trim() : null
  } catch {
    return null
  }
}

export function setToken(token: string): void {
  try {
    const v = token.trim()
    if (!v) localStorage.removeItem(TOKEN_STORAGE_KEY)
    else localStorage.setItem(TOKEN_STORAGE_KEY, v)
  } catch {
    // 隐私模式下 localStorage 不可写：退化为仅当前会话不可用，不抛错打断页面
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_STORAGE_KEY)
  } catch {
    /* 同上 */
  }
}

/** 有令牌才带 Authorization；无令牌返回原样 headers（匿名链路语义不变） */
export function authHeaders(init?: HeadersInit): HeadersInit {
  const token = getToken()
  if (!token) return init ?? {}
  const merged: Record<string, string> = { Authorization: `Bearer ${token}` }
  const base = init as Record<string, string> | undefined
  if (base && typeof base === "object" && !Array.isArray(base)) Object.assign(merged, base)
  return merged
}

/** 是否有令牌（供「未登录」提示与请求路径选择使用） */
export function hasToken(): boolean {
  return getToken() !== null
}

export interface TokenInfo {
  tenant: string | null
  subject: string | null
  expiresAt: number | null
  expired: boolean
  /** 解析失败原因（结构不对/不是 JWT）；合法但不是 JWT 时前端仍会原样发送 */
  error: string | null
}

function _b64urlDecode(seg: string): string | null {
  try {
    const pad = seg.length % 4 === 0 ? "" : "=".repeat(4 - (seg.length % 4))
    const b64 = seg.replace(/-/g, "+").replace(/_/g, "/") + pad
    const bin = atob(b64)
    // UTF-8 还原：避免中文 sub/tenant 变乱码
    const bytes = Uint8Array.from(bin, c => c.charCodeAt(0))
    return new TextDecoder().decode(bytes)
  } catch {
    return null
  }
}

function _str(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v.trim() : null
}

/**
 * 解 JWT payload 的 tenant_id/tenant 与 sub/exp（与后端 `_TENANT_CLAIMS` 对齐：两键并存且不一致时视为可疑）。
 * 仅用于展示；不验签、不判断角色。
 */
export function inspectToken(token?: string | null): TokenInfo {
  const raw = token === undefined ? getToken() : token
  const empty: TokenInfo = { tenant: null, subject: null, expiresAt: null, expired: false, error: null }
  if (!raw) return empty
  const segs = raw.split(".")
  if (segs.length !== 3) return { ...empty, error: "不是 JWT 三段结构（后端可能不接受）" }
  const payload = _b64urlDecode(segs[1] ?? "")
  if (!payload) return { ...empty, error: "payload 无法解码" }
  let claims: Record<string, unknown>
  try {
    claims = JSON.parse(payload) as Record<string, unknown>
  } catch {
    return { ...empty, error: "payload 不是合法 JSON" }
  }
  const a = _str(claims.tenant_id)
  const b = _str(claims.tenant)
  if (a && b && a !== b) return { ...empty, error: "tenant_id 与 tenant 声明不一致（后端会拒绝）" }
  const expRaw = claims.exp
  const expiresAt = typeof expRaw === "number" && Number.isFinite(expRaw) ? expRaw : null
  return {
    tenant: a ?? b,
    subject: _str(claims.sub),
    expiresAt,
    expired: expiresAt !== null ? expiresAt * 1000 <= Date.now() : false,
    error: null,
  }
}
