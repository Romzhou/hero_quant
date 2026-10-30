import {
  ROLE_RANK,
  TOKEN_CHANGED_EVENT,
  TOKEN_STORAGE_KEY,
  authHeaders,
  clearToken,
  getToken,
  hasRole,
  highestRole,
  inspectToken,
  normalizeRoles,
  notifyTokenChanged,
  roleLabel,
  setToken,
  type Role,
} from "../config/auth"

const b64url = (obj: unknown) =>
  btoa(JSON.stringify(obj)).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_")

const makeJwt = (payload: Record<string, unknown>) =>
  `${b64url({ alg: "RS256", typ: "JWT" })}.${b64url(payload)}.signature`

beforeEach(() => localStorage.clear())

test("setToken/getToken/clearToken 只落本机 localStorage", () => {
  expect(getToken()).toBeNull()
  setToken("  abc.def.ghi  ")
  expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe("abc.def.ghi")
  expect(getToken()).toBe("abc.def.ghi")
  setToken("   ")
  expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull()
  setToken("x.y.z")
  clearToken()
  expect(getToken()).toBeNull()
})

test("authHeaders 无令牌时原样返回，有令牌时追加 Bearer 且不覆盖调用方头", () => {
  const base = { Accept: "application/json" }
  expect(authHeaders(base)).toEqual(base)
  expect(authHeaders()).toEqual({})

  setToken("tok-123")
  expect(authHeaders(base)).toEqual({ Accept: "application/json", Authorization: "Bearer tok-123" })
  // 调用方显式头优先，避免被默认值盖掉
  expect(authHeaders({ Authorization: "Bearer explicit" })).toEqual({ Authorization: "Bearer explicit" })
})

test("inspectToken 解析 tenant_id/sub/exp；非 JWT/坏 JSON 给明确 error", () => {
  expect(inspectToken(null)).toEqual({
    tenant: null,
    subject: null,
    expiresAt: null,
    expired: false,
    roles: [],
    error: null,
  })

  expect(inspectToken("not-a-jwt").error).toMatch(/三段/)
  expect(inspectToken("a.%%%.c").error).toMatch(/payload/)
  expect(inspectToken(`a.${btoa("x")}.c`).error).toMatch(/JSON/)

  const exp = Math.floor(Date.now() / 1000) + 3600
  const info = inspectToken(makeJwt({ sub: "user-1", tenant_id: "tenant-a", exp }))
  expect(info).toMatchObject({ tenant: "tenant-a", subject: "user-1", expiresAt: exp, expired: false, error: null })
})

test("inspectToken 对 tenant_id/tenant 冲突与过期如实标记", () => {
  const mismatch = inspectToken(makeJwt({ sub: "u", tenant_id: "t1", tenant: "t2" }))
  expect(mismatch.error).toMatch(/不一致/)

  const expired = inspectToken(makeJwt({ sub: "u", tenant: "t1", exp: 1 }))
  expect(expired.tenant).toBe("t1")
  expect(expired.expired).toBe(true)
})

// --- PR-103 角色解析（口径对齐 src/hero_quant/auth/principal.py:96-118）---

/** 构造 TokenInfo：只关心 roles 的用例共用 */
const infoWith = (roles: Role[]) => ({ ...inspectToken(null), roles })

test("inspectToken: roles 优先、缺失才回退 realm_access.roles、两者都有时不并集", () => {
  // roles 存在 → 只用它
  expect(inspectToken(makeJwt({ sub: "u", tenant_id: "t", roles: ["analyst"] })).roles).toEqual(["analyst"])
  // roles 缺失 → 回退 Keycloak 的 realm_access.roles
  expect(inspectToken(makeJwt({ sub: "u", tenant_id: "t", realm_access: { roles: ["viewer", "admin"] } })).roles).toEqual([
    "viewer",
    "admin",
  ])
  // 两者都有 → 不并集：realm_access 里的 admin 不得把前端展示抬到比后端更宽
  const both = inspectToken(makeJwt({ sub: "u", tenant_id: "t", roles: ["viewer"], realm_access: { roles: ["admin"] } }))
  expect(both.roles).toEqual(["viewer"])
  expect(hasRole(both, "admin")).toBe(false)
  // roles: null 依后端口径视为「缺失」（`roles_raw is None` 才回退）
  expect(inspectToken(makeJwt({ sub: "u", tenant_id: "t", roles: null, realm_access: { roles: ["analyst"] } })).roles).toEqual([
    "analyst",
  ])
  // 解析失败 / 无任何声明 → []，绝不猜默认角色
  expect(inspectToken("not-a-jwt").roles).toEqual([])
  expect(inspectToken(makeJwt({ sub: "u", tenant_id: "t" })).roles).toEqual([])
  expect(inspectToken(null).roles).toEqual([])
})

test("角色归一：小写化、去重、按等级排序、逗号/空格字符串、未知值被过滤", () => {
  expect(normalizeRoles(["ADMIN", "viewer", "admin"])).toEqual(["viewer", "admin"])
  expect(normalizeRoles("analyst, VIEWER")).toEqual(["viewer", "analyst"])
  expect(normalizeRoles("viewer admin")).toEqual(["viewer", "admin"])
  expect(normalizeRoles(["superuser", 42, null, " Analyst "])).toEqual(["analyst"])
  expect(normalizeRoles(undefined)).toEqual([])
  expect(normalizeRoles({ roles: ["admin"] })).toEqual([])
  // 未知角色被过滤后不得放行任何 required
  expect(hasRole(inspectToken(makeJwt({ sub: "u", tenant_id: "t", roles: ["root", "superuser"] })), "viewer")).toBe(false)
})

test("hasRole 矩阵：viewer/analyst/admin × 三种 required，空数组不放行、admin 通吃", () => {
  const matrix: [Role[], Role, boolean][] = [
    // 声明 viewer
    [["viewer"], "viewer", true],
    [["viewer"], "analyst", false],
    [["viewer"], "admin", false],
    // 声明 analyst
    [["analyst"], "viewer", true],
    [["analyst"], "analyst", true],
    [["analyst"], "admin", false],
    // 声明 admin（通吃）
    [["admin"], "viewer", true],
    [["admin"], "analyst", true],
    [["admin"], "admin", true],
    // 空声明：一律不放行（advisory 走 RouteGuard 的另一条分支）
    [[], "viewer", false],
    [[], "analyst", false],
    [[], "admin", false],
    // 多角色：取最高
    [["viewer", "admin"], "admin", true],
  ]
  for (const [roles, required, expected] of matrix) {
    expect([roles, required, hasRole(infoWith(roles), required)]).toEqual([roles, required, expected])
  }
})

test("ROLE_RANK/roleLabel/highestRole 与后端等级同序", () => {
  expect(ROLE_RANK.viewer).toBeLessThan(ROLE_RANK.analyst)
  expect(ROLE_RANK.analyst).toBeLessThan(ROLE_RANK.admin)
  expect(roleLabel("analyst")).toBe("分析")
  expect(highestRole(infoWith(["viewer", "admin", "analyst"]))).toBe("admin")
  expect(highestRole(infoWith(["viewer"]))).toBe("viewer")
  expect(highestRole(infoWith([]))).toBeNull()
})

test("notifyTokenChanged 派发 hero-quant:token-changed（同标签页刷新角色用）", () => {
  const handler = vi.fn()
  window.addEventListener(TOKEN_CHANGED_EVENT, handler)
  notifyTokenChanged()
  expect(handler).toHaveBeenCalledTimes(1)
  window.removeEventListener(TOKEN_CHANGED_EVENT, handler)
  notifyTokenChanged()
  expect(handler).toHaveBeenCalledTimes(1)
})
