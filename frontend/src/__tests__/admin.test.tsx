import { render, screen, waitFor, cleanup } from "@testing-library/react"
import Admin from "../pages/admin/Admin"

const BASE: Record<string, unknown> = {
  overview: {
    scope: "platform",
    tenant_id: "tenant-a",
    runs: { total: 4, by_status: { queued: 1, running: 1, succeeded: 2 } },
    audit: { entries: 2 },
    queue: {
      oldest_queued_run_id: "run-1",
      oldest_queued_created_at: "2026-11-01T00:00:00+00:00",
      oldest_queued_wait_seconds: 12.5,
    },
    degraded: false,
  },
  tenants: {
    tenants: [{ tenant_id: "tenant-a", runs: 3, last_activity_at: "2026-11-01T00:00:00+00:00" }],
    count: 1,
    degraded: false,
  },
  runs: {
    runs: [
      {
        id: "run-1",
        tenant_id: "tenant-a",
        status: "queued",
        kind: "agent",
        attempt: 0,
        created_at: "2026-11-01T00:00:00+00:00",
      },
    ],
    count: 1,
    degraded: false,
  },
  quotas: {
    policy: { requests_per_minute: 60, window_seconds: 60, daily_token_budget: 1000 },
    tokens_used: 250,
    tokens_remaining: 750,
    usage_ratio: 0.25,
    unlimited: false,
    rate_window_used: null,
    rate_window_reason: "rate_window_not_exposed",
    degraded: false,
  },
  audit: {
    entries: 2,
    recent: [
      {
        id: 1,
        ts: "2026-11-01T00:00:00+00:00",
        action: "query.completed",
        user_id: "user-1",
        resource: "query",
        run_id: "run-1",
      },
    ],
    chain: {
      ok: true,
      broken_at: null,
      reason: null,
      degraded: false,
      reason_code: null,
      chain_start_entry_id: 10,
      chain_start_at: "2026-11-01T00:00:00+00:00",
      uncovered_count: 3,
    },
    degraded: false,
  },
  toggles: {
    toggles: [{ scope: "model", target: "m-1", tenant_id: "tenant-a", reason: "incident", actor: "user-1" }],
    count: 1,
    degraded: false,
  },
}

function endpointOf(url: string): string | null {
  if (url.includes("/overview")) return "overview"
  if (url.includes("/tenants")) return "tenants"
  if (url.includes("/runs")) return "runs"
  if (url.includes("/quotas")) return "quotas"
  if (url.includes("/audit/summary")) return "audit"
  if (url.includes("/toggles")) return "toggles"
  return null
}

/** 按端点分发；未覆盖的端点回 404（页面应显示错误而不是崩）。 */
function stubFetch(overrides: Record<string, unknown> = {}) {
  const fn = vi.fn(async (url: string) => {
    const key = endpointOf(url)
    if (key && (key in overrides || key in BASE)) {
      const payload = key in overrides ? overrides[key] : BASE[key]
      return { ok: true, status: 200, json: async () => payload }
    }
    return { ok: false, status: 404, json: async () => ({}) }
  })
  vi.stubGlobal("fetch", fn as unknown as typeof fetch)
  return fn
}

function stubForbidden() {
  const fn = vi.fn(async () => ({ ok: false, status: 403, json: async () => ({ detail: { reason: "admin_role_required" } }) }))
  vi.stubGlobal("fetch", fn as unknown as typeof fetch)
  return fn
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  cleanup()
})

test("degraded=true → 顶部横幅展示 reason", async () => {
  stubFetch({
    overview: { ...(BASE.overview as Record<string, unknown>), degraded: true, reason: "pg_unavailable" },
  })
  render(<Admin />)
  await waitFor(() => expect(screen.getByTestId("degraded-banner")).toBeInTheDocument())
  expect(screen.getByTestId("degraded-banner").textContent).toContain("总览：pg_unavailable")
})

test("缺失字段渲染 --，且不出现 NaN% / 假 0", async () => {
  stubFetch({
    overview: { runs: null, audit: null, queue: null, degraded: false },
    tenants: { tenants: null, count: null, degraded: true, reason: "cross_tenant_not_permitted" },
    runs: { runs: null, count: null, degraded: true, reason: "runs_unavailable" },
    quotas: { policy: null, tokens_used: null, tokens_remaining: null, usage_ratio: null, degraded: true, reason: "redis_unavailable" },
    audit: { entries: null, recent: null, chain: null, degraded: true, reason: "audit_store_unavailable" },
    toggles: { toggles: null, count: null, degraded: true, reason: "killswitch_unavailable" },
  })
  const { container } = render(<Admin />)
  await waitFor(() => expect(screen.getAllByText("--").length).toBeGreaterThan(0))
  expect(container.textContent).not.toContain("NaN%")
  expect(container.textContent).not.toContain("0.000")
  // 不可用必须是 null + reason，不能落成 0
  expect(screen.getByTestId("degraded-banner").textContent).toContain("redis_unavailable")
})

test("审计区按 chain_start_at / uncovered_count 表述，禁止「审计已启用」", async () => {
  stubFetch()
  const { container } = render(<Admin />)
  await waitFor(() =>
    expect(container.textContent).toContain("自 2026-11-01T00:00:00+00:00 起算，此前 3 条未覆盖"),
  )
  expect(container.textContent).not.toContain("审计已启用")
  // recent 行渲染真值
  expect(screen.getByText("query.completed")).toBeInTheDocument()
})

test("非 admin（403）→ 权限提示面板，不渲染数据区", async () => {
  stubForbidden()
  render(<Admin />)
  await waitFor(() => expect(screen.getByTestId("admin-403")).toBeInTheDocument())
  expect(screen.getByTestId("admin-403").textContent).toContain("权限不足（403）")
  expect(screen.queryByText("runs 只读看板")).not.toBeInTheDocument()
})

test("正常数据：总览、配额与开关渲染真值", async () => {
  stubFetch()
  render(<Admin />)
  await waitFor(() => expect(screen.getAllByText("run-1").length).toBeGreaterThan(0))
  expect(screen.getByText("25.0%")).toBeInTheDocument()
  expect(screen.getByText("250 / 1000")).toBeInTheDocument()
  expect(screen.getAllByText("model").length).toBeGreaterThan(0)
  expect(screen.queryByTestId("degraded-banner")).not.toBeInTheDocument()
})
