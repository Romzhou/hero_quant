/**
 * PR-172 合规文本产品内入口（Compliance）单测
 *
 * 锁定四件事：
 * 1. `/compliance` 只读页在无令牌、低权令牌下都能渲染（不进 RouteGuard，任何角色可达）；
 * 2. 五个分区标题齐全，版本号 0.2.0 与「与 docs/COMPLIANCE.md 同步」的整句在页面上；
 * 3. 反向断言：不得出现「认证 / 批准 / 保证收益 / 保本 / 零风险」等未发生或过度承诺的表述；
 * 4. 入口存在：App 页脚链接 href=/compliance（且原有「不构成投资建议」保留）+ 设置页令牌卡片旁链接。
 */
import { cleanup, render, screen, within } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import App from "../App"
import Settings from "../pages/Settings"
import { COMPLIANCE_DOC_ALIGNED_AT, COMPLIANCE_DOC_REF, COMPLIANCE_DOC_VERSION, COMPLIANCE_SECTIONS } from "../pages/Compliance"
import { setToken } from "../config/auth"
import { useSettingsStore } from "../store/settings"

/** 与 roleGuard.test.tsx / authEntry.test.tsx 同款：只造结构合法的 JWT，不验签 */
const b64url = (obj: unknown) =>
  btoa(JSON.stringify(obj)).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_")
const makeJwt = (payload: Record<string, unknown>) =>
  `${b64url({ alg: "RS256", typ: "JWT" })}.${b64url(payload)}.signature`

const renderApp = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>
  )

/** 五个分区标题：需求写死的清单，同时用 COMPLIANCE_SECTIONS 交叉校验防漂移 */
const EXPECTED_TITLES = ["投资免责", "数据来源与限制", "合成模式", "隐私与令牌", "版本与同步"]

/** 未发生/过度承诺的表述：命中即视为夸大 */
const FORBIDDEN = /认证|批准|保证收益|保本|稳赚|零风险|已通过|承诺收益/

beforeEach(() => {
  localStorage.clear()
  // Shell 会探活 /ready；给稳定成功响应，避免真实网络噪声（与 roleGuard.test.tsx 一致）
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ status: "ok" }) }))
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
  useSettingsStore.setState({ apiBase: "", model: "gpt-4o-mini" })
})

test("五个分区标题、版本号 0.2.0 与同步声明都在页面上", async () => {
  renderApp("/compliance")
  expect(await screen.findByRole("heading", { name: "合规与免责" }, { timeout: 4000 })).toBeInTheDocument()

  // 分区标题（顺序即 COMPLIANCE_SECTIONS 顺序，便于文档升版时对表）
  expect(COMPLIANCE_SECTIONS.map(s => s.title)).toEqual(EXPECTED_TITLES)
  for (const title of EXPECTED_TITLES) {
    expect(screen.getByRole("heading", { name: title })).toBeInTheDocument()
  }

  // 版本：文档版本 0.2.0 + 权威文本位置 + 静态对齐日期（禁止 new Date() 造成的漂移）
  expect(COMPLIANCE_DOC_VERSION).toBe("0.2.0")
  expect(COMPLIANCE_DOC_REF).toBe("docs/COMPLIANCE.md")
  expect(COMPLIANCE_DOC_ALIGNED_AT).toBe("2026-10-31")
  expect(screen.getByTestId("compliance-version")).toHaveTextContent(
    "合规文本版本 0.2.0 · 与 docs/COMPLIANCE.md 同步"
  )
  expect(screen.getByTestId("compliance-page")).toHaveTextContent("2026-10-31")
})

test("反向断言：不出现认证/批准/保证收益等未发生或过度承诺的表述", async () => {
  renderApp("/compliance")
  await screen.findByRole("heading", { name: "合规与免责" }, { timeout: 4000 })
  const pageText = screen.getByTestId("compliance-page").textContent || ""
  expect(pageText).not.toMatch(FORBIDDEN)
  // 关键正向锚点：免责/幻觉需复核/顺序回退非择优/合成打标/后端裁决/未验签
  expect(pageText).toContain("非投资建议")
  expect(pageText).toContain("关键结论仍需人工复核")
  expect(pageText).toContain("顺序回退不等于择优")
  expect(pageText).toContain("provenance.source=synthetic")
  expect(pageText).toContain("live 取数失败不会静默回退")
  expect(pageText).toContain("后端裁决")
  expect(pageText).toContain("未验签")
})

test("无令牌时合规页照常渲染：不被 RouteGuard 拦截，也不出 advisory", async () => {
  renderApp("/compliance")
  expect(await screen.findByRole("heading", { name: "合规与免责" }, { timeout: 4000 })).toBeInTheDocument()
  // 该路由根本没包 RouteGuard：既无 403 面板，也无「未提供令牌」advisory
  expect(screen.queryByTestId("route-guard-403")).toBeNull()
  expect(screen.queryByTestId("route-guard-advisory")).toBeNull()
  expect(screen.getByRole("heading", { name: "投资免责" })).toBeInTheDocument()
})

test("低权令牌（viewer）也不拦截：合规文本对任何角色可读", async () => {
  setToken(makeJwt({ sub: "user-1", tenant_id: "tenant-a", roles: ["viewer"] }))
  renderApp("/compliance")
  expect(await screen.findByRole("heading", { name: "合规与免责" }, { timeout: 4000 })).toBeInTheDocument()
  expect(screen.queryByTestId("route-guard-403")).toBeNull()
  // 对照：viewer 访问需要 analyst 的 /backtest 时会出现 403 面板（证明上面不是恒真）
  cleanup()
  renderApp("/backtest")
  expect(await screen.findByTestId("route-guard-403", {}, { timeout: 4000 })).toBeInTheDocument()
})

test("App 页脚有「合规与免责」链接指向 /compliance，原有「不构成投资建议」保留", async () => {
  renderApp("/compliance")
  await screen.findByRole("heading", { name: "合规与免责" }, { timeout: 4000 })
  const footer = document.querySelector("footer") as HTMLElement
  expect(footer).not.toBeNull()
  const link = within(footer).getByRole("link", { name: "合规与免责" })
  expect(link).toHaveAttribute("href", "/compliance")
  expect(footer.textContent || "").toContain("不构成投资建议")
})

test("设置页令牌卡片旁有「查看合规与免责说明」链接", async () => {
  render(<Settings />)
  const link = await screen.findByTestId("settings-compliance-link")
  expect(link).toHaveAttribute("href", "/compliance")
  expect(link).toHaveTextContent("查看合规与免责说明")
  // 链接必须落在「身份令牌」卡片里，而不是页面别处
  const tokenCard = link.closest("div.rounded-2xl") as HTMLElement
  expect(tokenCard.textContent || "").toContain("身份令牌")
})
