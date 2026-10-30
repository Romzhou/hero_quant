/**
 * 前端身份入口（E线 t14 批次2 / PR-103 角色提示）
 * - 只做三件事：存令牌、把令牌挂到请求头、把令牌里的声明解出来给人看。
 * - 不验签、不裁决权限：令牌是否合法、租户是否有效、角色是否被接受一律由后端裁决，
 *   前端解析结果仅供展示与 UX 提示，必须在 UI 上标注「来自令牌声明，未经服务端确认」。
 * - 角色解析严格对齐后端 `src/hero_quant/auth/principal.py:117-121`（roles 优先、缺失才回退
 *   realm_access.roles、不并集），避免前端展示比后端实际权限更宽。
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

/**
 * 令牌变更事件：同一标签页内 localStorage 不会触发 `storage` 事件，
 * 因此由 Settings 保存/清除后显式 notify，App/RouteGuard 监听后重读角色。
 * 不引入全局状态库（zustand 只管设置项），单一事件名集中在此防漂移。
 */
export const TOKEN_CHANGED_EVENT = "hero-quant:token-changed"

export function notifyTokenChanged(): void {
  try {
    window.dispatchEvent(new CustomEvent(TOKEN_CHANGED_EVENT))
  } catch {
    /* 非浏览器环境（SSR/测试缺失 window）静默：事件仅用于刷新展示 */
  }
}

/** 角色白名单：与后端 `auth.principal.Role` 一一对应（顺序即等级升序） */
export const ROLES = ["viewer", "analyst", "admin"] as const

export type Role = (typeof ROLES)[number]

/** 等级包含判定用权重：与后端 `_ROLE_RANK`（principal.py:35）同值 */
export const ROLE_RANK: Record<Role, number> = { viewer: 1, analyst: 2, admin: 3 }

/** 角色中文标签（仅展示，不代表任何服务端裁决） */
export const ROLE_LABEL: Record<Role, string> = { viewer: "只读", analyst: "分析", admin: "管理员" }

export function roleLabel(role: Role): string {
  return ROLE_LABEL[role] ?? role
}

/**
 * 归一并过滤 roles claim（对齐后端 `_normalize_roles`，principal.py:58-76）：
 * 支持数组/字符串（空格或逗号分隔）、小写化、只留白名单值、去重、按等级升序排序。
 * 未知角色忽略而不报错——前端绝不能凭未知值推断出更宽的权限。
 */
export function normalizeRoles(raw: unknown): Role[] {
  const items: unknown[] = typeof raw === "string" ? raw.replace(/,/g, " ").split(/\s+/) : Array.isArray(raw) ? raw : []
  const seen = new Set<Role>()
  for (const item of items) {
    if (typeof item !== "string") continue
    const key = item.trim().toLowerCase()
    if ((ROLES as readonly string[]).includes(key)) seen.add(key as Role)
  }
  return ROLES.filter(r => seen.has(r))
}

/**
 * 取角色声明来源：与后端 `Principal.from_claims`（principal.py:117-121）一致——
 * `roles` 存在（非 null/undefined）就只用它，缺失才回退 `realm_access.roles`；**不并集**。
 */
function _roleClaim(claims: Record<string, unknown>): unknown {
  const direct = claims.roles
  if (direct !== undefined && direct !== null) return direct
  const realm = claims.realm_access
  if (realm && typeof realm === "object" && !Array.isArray(realm)) {
    return (realm as Record<string, unknown>).roles
  }
  return undefined
}

export interface TokenInfo {
  tenant: string | null
  subject: string | null
  expiresAt: number | null
  expired: boolean
  /** 令牌声明的角色（已归一/过滤/排序）；缺声明或解析失败为 []，绝不猜默认角色 */
  roles: Role[]
  /** 解析失败原因（结构不对/不是 JWT）；合法但不是 JWT 时前端仍会原样发送 */
  error: string | null
}

/**
 * 角色满足判定（与后端 `Principal.has_role`，principal.py:99-102 同语义）：
 * 等级单向包含——admin 满足 analyst/viewer，analyst 满足 viewer。
 * 仅用于 UI 提示与「先别发请求」，不构成安全边界。
 */
export function hasRole(info: TokenInfo, required: Role): boolean {
  const need = ROLE_RANK[required] ?? 0
  return info.roles.some(role => (ROLE_RANK[role] ?? 0) >= need)
}

/** 令牌声明的最高等级角色（顶栏 chip 用）；无角色声明返回 null */
export function highestRole(info: TokenInfo): Role | null {
  let top: Role | null = null
  for (const role of info.roles) {
    if (top === null || (ROLE_RANK[role] ?? 0) > (ROLE_RANK[top] ?? 0)) top = role
  }
  return top
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
 * 解 JWT payload 的 tenant_id/tenant、sub/exp 与 roles（与后端 `_TENANT_CLAIMS` 对齐：两键并存且不一致时视为可疑）。
 * 仅用于展示与 UX 守卫；不验签、不裁决权限。
 */
export function inspectToken(token?: string | null): TokenInfo {
  const raw = token === undefined ? getToken() : token
  const empty: TokenInfo = { tenant: null, subject: null, expiresAt: null, expired: false, roles: [], error: null }
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
    roles: normalizeRoles(_roleClaim(claims)),
    error: null,
  }
}
