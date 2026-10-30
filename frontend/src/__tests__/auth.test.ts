import {
  TOKEN_STORAGE_KEY,
  authHeaders,
  clearToken,
  getToken,
  inspectToken,
  setToken,
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
