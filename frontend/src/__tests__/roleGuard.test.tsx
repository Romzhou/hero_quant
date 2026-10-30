/**
 * PR-103：前端路由守卫 / 角色提示测试
 *
 * 覆盖三档行为（与 RouteGuard.tsx 头注释一一对应）：
 * 1. 无令牌 → advisory 放行（后端可能未开鉴权，硬拦会锁死开发环境）；
 * 2. 有令牌但无 roles → advisory 放行（前端无从判定，不猜默认角色）；
 * 3. 有令牌且 role 明确不足 → 403 面板拦截，children 不渲染（不发请求）。
 * 并验证 App 接线：/chat,/backtest=analyst；其余=viewer；/settings 永远可达。
 */
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import RouteGuard from "../components/RouteGuard"
import App from "../App"
import { notifyTokenChanged, setToken, type Role } from "../config/auth"

const b64url = (obj: unknown) =>
  btoa(JSON.stringify(obj)).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_")

const makeJwt = (payload: Record<string, unknown>) =>
  `${b64url({ alg: "RS256", typ: "JWT" })}.${b64url(payload)}.signature`

const tokenWith = (roles?: unknown) =>
  makeJwt({ sub: "user-1", tenant_id: "tenant-a", roles })

const renderGuard = (required: Role) =>
  render(
    <MemoryRouter>
      <RouteGuard required={required} title="回测">
        <div data-testid="page-content">page-content</div>
      </RouteGuard>
    </MemoryRouter>
  )

const renderApp = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>
  )

/** 只查主导航：页面正文里也有「去研究/实盘监控/风控」类快捷链接，全局查询会命中多个 */
const nav = () => within(screen.getByRole("navigation", { name: "Primary" }))

beforeEach(() => {
  localStorage.clear()
  // Shell 会探活 /ready；给个稳定成功响应，避免真实网络噪声
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ status: "ok" }) }))
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
})

// --- RouteGuard 三档分支 ---

test("无令牌：advisory 放行，children 照常渲染（不锁死未开鉴权的开发环境）", () => {
  renderGuard("analyst")
  const advisory = screen.getByTestId("route-guard-advisory")
  expect(advisory).toHaveTextContent("未提供令牌")
  expect(advisory).toHaveTextContent("401/403")
  expect(advisory).toHaveTextContent("未验签")
  expect(screen.getByTestId("page-content")).toBeInTheDocument()
  expect(screen.queryByTestId("route-guard-403")).toBeNull()
})

test("有令牌但 roles 为空：advisory 放行，文案标明权限以服务端为准", () => {
  setToken(makeJwt({ sub: "user-1", tenant_id: "tenant-a" }))
  renderGuard("analyst")
  const advisory = screen.getByTestId("route-guard-advisory")
  expect(advisory).toHaveTextContent("令牌未声明角色")
  expect(advisory).toHaveTextContent("以服务端为准")
  expect(screen.getByTestId("page-content")).toBeInTheDocument()
  expect(screen.queryByTestId("route-guard-403")).toBeNull()
})

test("未知角色被过滤后等价于「无角色声明」→ advisory 放行而非 403", () => {
  setToken(tokenWith(["superuser"]))
  renderGuard("viewer")
  expect(screen.getByTestId("route-guard-advisory")).toHaveTextContent("令牌未声明角色")
  expect(screen.getByTestId("page-content")).toBeInTheDocument()
})

test("viewer 访问 analyst 页面：403 面板拦截，children 不渲染且不发请求", () => {
  setToken(tokenWith(["viewer"]))
  renderGuard("analyst")
  expect(screen.queryByTestId("page-content")).toBeNull()
  const panel = screen.getByTestId("route-guard-403")
  expect(panel).toHaveTextContent("需要")
  expect(panel).toHaveTextContent("analyst")
  expect(panel).toHaveTextContent("及以上")
  expect(panel).toHaveTextContent("viewer")
  expect(panel).toHaveTextContent("未验签")
  expect(screen.getByRole("link", { name: /设置页更换令牌/ })).toHaveAttribute("href", "/settings")
  // 拦截＝不发请求：除 Shell 的 /ready 外不应有 /v1 调用
  const urls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.map(c => String(c[0]))
  expect(urls.some(u => u.startsWith("/v1"))).toBe(false)
})

test("analyst 访问 analyst 页面：正常渲染，无 advisory/403", () => {
  setToken(tokenWith(["analyst"]))
  renderGuard("analyst")
  expect(screen.getByTestId("page-content")).toBeInTheDocument()
  expect(screen.queryByTestId("route-guard-advisory")).toBeNull()
  expect(screen.queryByTestId("route-guard-403")).toBeNull()
})

test("admin 访问 analyst 页面：admin 通吃，正常渲染", () => {
  setToken(tokenWith(["admin"]))
  renderGuard("analyst")
  expect(screen.getByTestId("page-content")).toBeInTheDocument()
  expect(screen.queryByTestId("route-guard-403")).toBeNull()
})

test("令牌变更事件后重新判定：viewer → analyst 后同一页面放行", async () => {
  setToken(tokenWith(["viewer"]))
  renderGuard("analyst")
  expect(screen.getByTestId("route-guard-403")).toBeInTheDocument()

  act(() => {
    setToken(tokenWith(["analyst"]))
    notifyTokenChanged()
  })
  await waitFor(() => expect(screen.getByTestId("page-content")).toBeInTheDocument())
  expect(screen.queryByTestId("route-guard-403")).toBeNull()
})

// --- App 接线 ---

test("App /chat：viewer 令牌 → 出现 403 面板，Chat 不渲染", async () => {
  setToken(tokenWith(["viewer"]))
  renderApp("/chat")
  expect(await screen.findByTestId("route-guard-403", {}, { timeout: 4000 })).toBeInTheDocument()
  expect(screen.queryByText(/投研对话/)).toBeNull()
})

test("App /chat：analyst 令牌 → 正常渲染 Chat", async () => {
  setToken(tokenWith(["analyst"]))
  renderApp("/chat")
  expect(await screen.findByText(/投研对话/, {}, { timeout: 4000 })).toBeInTheDocument()
  expect(screen.queryByTestId("route-guard-403")).toBeNull()
})

test("App /backtest：viewer 令牌 → 403（与 /chat 同属 analyst 档）", async () => {
  setToken(tokenWith(["viewer"]))
  renderApp("/backtest")
  expect(await screen.findByTestId("route-guard-403", {}, { timeout: 4000 })).toBeInTheDocument()
})

test("App /dashboard：viewer 令牌正常；无令牌时 advisory 放行", async () => {
  setToken(tokenWith(["viewer"]))
  const first = renderApp("/dashboard")
  expect(await screen.findByText(/Dashboard/i, {}, { timeout: 4000 })).toBeInTheDocument()
  expect(screen.queryByTestId("route-guard-advisory")).toBeNull()
  first.unmount()

  localStorage.clear()
  renderApp("/dashboard")
  expect(await screen.findByTestId("route-guard-advisory", {}, { timeout: 4000 })).toHaveTextContent("未提供令牌")
  expect(await screen.findByText(/Dashboard/i, {}, { timeout: 4000 })).toBeInTheDocument()
})

test("App /settings：不设角色闸，viewer 令牌下依旧可达（换令牌自救）", async () => {
  setToken(tokenWith(["viewer"]))
  renderApp("/settings")
  expect(await screen.findByRole("heading", { name: "设置" }, { timeout: 4000 })).toBeInTheDocument()
  expect(screen.queryByTestId("route-guard-403")).toBeNull()
})

// --- 导航锁定与角色 chip ---

test("导航：viewer 令牌下 回测（analyst 档）加 aria-disabled + 锁形提示，其余 viewer 档不加", () => {
  setToken(tokenWith(["viewer"]))
  renderApp("/dashboard")
  const backtest = nav().getByRole("link", { name: /回测/ })
  expect(backtest).toHaveAttribute("aria-disabled", "true")
  expect(backtest).toHaveAttribute("data-locked", "true")
  expect(backtest).toHaveAttribute("title", expect.stringContaining("analyst"))
  for (const label of [/看板/, /研究/, /实盘/, /监控/, /风控/]) {
    const link = nav().getByRole("link", { name: label })
    expect(link).not.toHaveAttribute("aria-disabled")
    expect(link).not.toHaveAttribute("data-locked")
  }
  // 不隐藏：链接仍在导航里可达
  expect(backtest).toHaveAttribute("href", "/backtest")
})

test("导航：advisory 状态（无令牌 / 令牌无 roles）不动导航", () => {
  renderApp("/dashboard")
  for (const label of [/看板/, /研究/, /回测/, /实盘/, /监控/, /风控/]) {
    expect(nav().getByRole("link", { name: label })).not.toHaveAttribute("aria-disabled")
  }

  cleanup()
  setToken(makeJwt({ sub: "u", tenant_id: "tenant-a" }))
  renderApp("/dashboard")
  for (const label of [/看板/, /研究/, /回测/, /实盘/, /监控/, /风控/]) {
    expect(nav().getByRole("link", { name: label })).not.toHaveAttribute("aria-disabled")
  }
})

test("顶栏角色 chip：未验签声明原样展示，无令牌/无角色各自如实标注", () => {
  setToken(tokenWith(["viewer", "analyst"]))
  const first = renderApp("/dashboard")
  // 取最高等级展示，且必须标明未验签
  expect(screen.getByTestId("role-chip")).toHaveTextContent("角色：analyst（未验签）")
  expect(screen.getByTestId("role-chip")).toHaveAttribute("title", expect.stringContaining("未验签"))
  first.unmount()

  localStorage.clear()
  renderApp("/dashboard")
  expect(screen.getByTestId("role-chip")).toHaveTextContent("未提供令牌")

  cleanup()
  setToken(makeJwt({ sub: "u", tenant_id: "tenant-a" }))
  renderApp("/dashboard")
  expect(screen.getByTestId("role-chip")).toHaveTextContent("角色未声明")
})

test("令牌变更事件会刷新 chip（设置页保存后无需刷新页面）", async () => {
  renderApp("/dashboard")
  expect(screen.getByTestId("role-chip")).toHaveTextContent("未提供令牌")

  act(() => {
    setToken(tokenWith(["admin"]))
    notifyTokenChanged()
  })
  await waitFor(() => expect(screen.getByTestId("role-chip")).toHaveTextContent("角色：admin（未验签）"))
})
