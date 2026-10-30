import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import Settings from "../pages/Settings"
import { useSettingsStore } from "../store/settings"
import { TOKEN_CHANGED_EVENT, TOKEN_STORAGE_KEY } from "../config/auth"

const b64url = (obj: unknown) =>
  btoa(JSON.stringify(obj)).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_")

const makeJwt = (payload: Record<string, unknown>) =>
  `${b64url({ alg: "RS256", typ: "JWT" })}.${b64url(payload)}.signature`

beforeEach(() => {
  localStorage.clear()
  useSettingsStore.setState({ apiBase: "", model: "gpt-4o-mini" })
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200 } as any))
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

test("设置页可保存/展示/清除令牌，并声明声明值未经服务端确认", async () => {
  render(<Settings />)
  expect(screen.getByTestId("token-status")).toHaveTextContent("未配置")

  const token = makeJwt({ sub: "user-1", tenant_id: "tenant-a", exp: Math.floor(Date.now() / 1000) + 3600 })
  fireEvent.change(screen.getByPlaceholderText(/粘贴 JWT/), { target: { value: token } })
  fireEvent.click(screen.getByRole("button", { name: "保存令牌" }))

  await waitFor(() => expect(screen.getByTestId("token-status")).toHaveTextContent("已配置"))
  expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe(token)
  expect(screen.getByTestId("token-tenant")).toHaveTextContent("tenant-a")
  expect(screen.getByTestId("token-subject")).toHaveTextContent("user-1")
  expect(document.body.textContent).toContain("未经服务端确认")

  fireEvent.click(screen.getByRole("button", { name: "清除" }))
  await waitFor(() => expect(screen.getByTestId("token-status")).toHaveTextContent("未配置"))
  expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull()
  expect(screen.queryByTestId("token-tenant")).toBeNull()
})

test("设置页对非 JWT 令牌显示解析错误但仍允许保存（有效性由后端裁决）", async () => {
  render(<Settings />)
  fireEvent.change(screen.getByPlaceholderText(/粘贴 JWT/), { target: { value: "not-a-jwt" } })
  fireEvent.click(screen.getByRole("button", { name: "保存令牌" }))

  await waitFor(() => expect(screen.getByTestId("token-status")).toHaveTextContent("已配置"))
  expect(document.body.textContent).toMatch(/三段/)
  expect(screen.getByTestId("token-tenant")).toHaveTextContent("—")
})

test("保存/清除令牌后派发 hero-quant:token-changed，并展示令牌声明的角色", async () => {
  const handler = vi.fn()
  window.addEventListener(TOKEN_CHANGED_EVENT, handler)
  const token = makeJwt({ sub: "user-1", tenant_id: "tenant-a", roles: ["viewer", "analyst"] })

  render(<Settings />)
  fireEvent.change(screen.getByPlaceholderText(/粘贴 JWT/), { target: { value: token } })
  fireEvent.click(screen.getByRole("button", { name: "保存令牌" }))

  await waitFor(() => expect(screen.getByTestId("token-status")).toHaveTextContent("已配置"))
  // 角色按等级升序展示，附中文标签，仍标注来自声明
  expect(screen.getByTestId("token-roles")).toHaveTextContent("viewer（只读）、analyst（分析）")
  expect(handler).toHaveBeenCalledTimes(1)

  fireEvent.click(screen.getByRole("button", { name: "清除" }))
  await waitFor(() => expect(screen.getByTestId("token-status")).toHaveTextContent("未配置"))
  expect(handler).toHaveBeenCalledTimes(2)
  window.removeEventListener(TOKEN_CHANGED_EVENT, handler)
})
