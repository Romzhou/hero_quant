/**
 * 前端真实性单测（E线 / S6）
 * - trace 判定：合成心跳不得摘掉监控页「演示数据」标记，只有 non_synthetic:true 或带 ts 的真实记录才算真实
 * - Chat：空会话不得出现写死的工具延迟（42ms/180ms），只显示「暂无工具调用」
 * - Research：引擎权重 schema（date,asset_0,asset_1）无 close 列时必须如实提示，而不是伪造曲线
 */
import { render, cleanup, waitFor } from "@testing-library/react"
import { isRealTraceEvent } from "../config/trace"
import Chat from "../pages/Chat"
import Research from "../pages/Research"
import { useChatStore } from "../store/chat"

vi.mock("echarts-for-react", () => ({
  default: (props: any) => <div data-testid="echarts-mock" data-option={JSON.stringify(props.option?.series ?? "").slice(0, 120)} />,
}))

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  cleanup()
})

test("isRealTraceEvent: 合成心跳与无证据载荷一律判为演示", () => {
  expect(isRealTraceEvent({ type: "otel", msg: "heartbeat · events.jsonl offset 0", ts: "" })).toBe(false)
  expect(isRealTraceEvent({ type: "trace", msg: "x" })).toBe(false)
  expect(isRealTraceEvent({ type: "trace", msg: "x", synthetic: true, ts: "2026-10-28T10:00:00Z" })).toBe(false)
  expect(isRealTraceEvent(null)).toBe(false)
  expect(isRealTraceEvent({ type: "tool", tool: "get_market_data", msg: "real", ts: "2026-10-28T10:00:00Z" })).toBe(true)
  expect(isRealTraceEvent({ type: "tool", msg: "real", non_synthetic: true })).toBe(true)
})

test("Chat 空会话不伪造工具延迟，只显示暂无工具调用", () => {
  useChatStore.getState().clear()
  Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() })
  render(<Chat />)
  const txt = document.body.textContent || ""
  expect(txt).toContain("暂无工具调用")
  expect(txt).not.toContain("42ms")
  expect(txt).not.toContain("180ms")
})

test("Research 权重表无 close 列时如实提示，不渲染伪造净值", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    text: async () => "date,asset_0,asset_1\n2026-08-12,0.5,0.5\n2026-08-13,0.5,0.5\n",
    json: async () => ({ sharpe: 0.9, synthetic: true, degraded: true, reason: "backtest:synthetic_demo", monthly_returns: { "2026-08": 0.01 } }),
  }))
  render(<Research />)
  await waitFor(() => expect(document.body.textContent || "").toContain("无 close 列"))
  expect(textHasSeries()).toBe(false)
})

function textHasSeries(): boolean {
  const el = document.querySelector('[data-testid="echarts-mock"]') as HTMLElement | null
  if (!el) return false
  return (el.getAttribute("data-option") || "").includes("累积收益")
}
