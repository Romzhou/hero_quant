/**
 * E线 t14 批次1：失败语义锁定（vitest）
 * - 背景：D 线（t10）接线后 401/403/429/kill switch 503 是真实拒绝。此前页面把这些一律
 *   `throw new Error(String(status))` 后静默回退 mock，只在角标写 isMock——等于把「未登录」
 *   和「服务被熔断」伪装成「演示模式」。
 * - 锁定：① 状态码+机器码 → 失败分类 ② 2xx 里的 degraded/failover/emulated 仍归降级
 *   ③ 横幅按 kind 分级（红=失败 / 琥珀=降级）④ Chat 与 Research 集成真实点亮失败横幅
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach } from "vitest"

// jsdom 无 canvas：echarts 真渲染会抛未捕获异常，按既有 Research 测试同款方式 mock
vi.mock("echarts-for-react", () => ({
  default: (props: any) => <div data-testid="echarts-mock" data-option={JSON.stringify(props.option?.series ?? null).slice(0, 200)} />,
}))
import DegradedBanner from "../components/DegradedBanner"
import {
  describeHttpFailure,
  extractDegradedFromResponse,
  type HttpFailure,
} from "../config/degraded"
import Chat from "../pages/Chat"
import Research from "../pages/Research"
import { useChatStore } from "../store/chat"

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  cleanup()
})

const jsonResp = (status: number, body: unknown, headers?: Record<string, string>) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
  text: async () => JSON.stringify(body),
  headers: { get: (k: string) => headers?.[k] ?? null },
})

// ---------- 分类：状态码 + 后端机器码 ----------

test("401 → 未认证，直显后端 detail/code", () => {
  const f = describeHttpFailure(401, { detail: "missing_authorization_header", code: "invalid_token" })
  expect(f?.kind).toBe("unauthorized")
  expect(f?.message).toContain("missing_authorization_header")
})

test("403 → 权限不足；429 → 配额且带 Retry-After", () => {
  expect(describeHttpFailure(403, { detail: "analyst required", code: "insufficient_role" })?.kind).toBe("forbidden")
  const q = describeHttpFailure(429, { code: "quota_exceeded" }, "30") as HttpFailure
  expect(q.kind).toBe("quota")
  expect(q.retryAfterSeconds).toBe(30)
})

test("503 killswitch_active → killswitch，取人类可读 kill_reason", () => {
  const f = describeHttpFailure(503, { detail: { reason: "killswitch_active", kill_reason: "模型 gpt-4o 已停用" } })
  expect(f?.kind).toBe("killswitch")
  expect(f?.message).toContain("模型 gpt-4o 已停用")
})

test("503 killswitch_unavailable 也按 fail-closed 归 killswitch，不混成普通 503", () => {
  const f = describeHttpFailure(503, { detail: { reason: "killswitch_unavailable", detail: "redis down" } })
  expect(f?.kind).toBe("killswitch")
  expect(f?.message).toContain("redis down")
})

test("503 auth_required_for_sessions → 未认证；其它 5xx → 服务不可用", () => {
  expect(describeHttpFailure(503, { detail: { reason: "auth_required_for_sessions" } })?.kind).toBe("unauthorized")
  expect(describeHttpFailure(500, { detail: "boom" })?.kind).toBe("unavailable")
})

test("2xx/404/400 不算系统级失败（返回 null，由页面空态处理）", () => {
  expect(describeHttpFailure(200, {})).toBeNull()
  expect(describeHttpFailure(404, { detail: { reason: "session_not_found" } })).toBeNull()
  expect(describeHttpFailure(400, { detail: { reason: "session_invalid_request" } })).toBeNull()
})

// ---------- 2xx 响应里的降级标记 ----------

test("degraded_reason 优先，并补译 failover / emulated 两个机器态", () => {
  expect(extractDegradedFromResponse({ degraded: true, degraded_reason: "fake llm" })).toBe("fake llm")
  const mixed = extractDegradedFromResponse({ llm_mode: "failover", checkpoint_mode: "emulated" })
  expect(mixed).toContain("备用 provider")
  expect(mixed).toContain("checkpoint 未接 Postgres")
  expect(extractDegradedFromResponse({ delta: "hi" })).toBeNull()
})

// ---------- 横幅分级 ----------

test("失败横幅与降级横幅必须可区分（testid + kind + 标题）", () => {
  const { unmount } = render(<DegradedBanner failure={{ kind: "killswitch", message: "已停用" }} />)
  const banner = screen.getByTestId("failure-banner")
  expect(banner.dataset.failureKind).toBe("killswitch")
  expect(banner.textContent).toContain("服务已熔断")
  expect(screen.queryByTestId("degraded-banner")).toBeNull()
  unmount()

  render(<DegradedBanner reason="fake llm" />)
  expect(screen.getByTestId("degraded-banner").textContent).toContain("演示 / 降级模式")
})

// ---------- Chat 集成 ----------

test("Chat 票据接口 401 → 失败横幅（不得只显示演示数据）", async () => {
  useChatStore.getState().clear()
  useChatStore.getState().setInput("")
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResp(401, { detail: "missing_authorization_header", code: "invalid_token" })))
  render(<Chat />)
  fireEvent.change(screen.getByPlaceholderText(/输入投研问题/), { target: { value: "回测 600519" } })
  fireEvent.click(screen.getByRole("button", { name: "发送" }))
  const banner = await screen.findByTestId("failure-banner", {}, { timeout: 4000 })
  expect(banner.dataset.failureKind).toBe("unauthorized")
  expect(banner.textContent).toContain("missing_authorization_header")
})

// ---------- Research 集成 ----------

test("Research 回测产物 503 熔断 → 失败横幅，且不冒充真实数据", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(jsonResp(503, { detail: { reason: "killswitch_active", kill_reason: "租户已停用" } })),
  )
  render(<Research />)
  const banner = await screen.findByTestId("failure-banner", {}, { timeout: 4000 })
  expect(banner.dataset.failureKind).toBe("killswitch")
  expect(banner.textContent).toContain("租户已停用")
  await waitFor(() => expect(screen.getByText(/当前指标为演示数据/)).toBeInTheDocument())
})
