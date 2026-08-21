import { render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import App from "../App"

const expectRoute = async (path: string, pattern: RegExp) => {
  const { findByText, unmount } = render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>
  )
  // Suspense lazy -> findByText async (isolated to this render)
  const el = await findByText(pattern, {}, { timeout: 4000 })
  expect(el).toBeInTheDocument()
  unmount()
}

describe("5 routes Dashboard/Research/Backtest/Live/Risk", () => {
  test("dashboard reachable", async () => {
    await expectRoute("/dashboard", /Dashboard/i)
  })
  test("research reachable", async () => {
    await expectRoute("/research", /研究/)
  })
  test("backtest reachable", async () => {
    // Backtest is Chat page with 投研对话
    await expectRoute("/backtest", /对话|Backtest|投研对话/)
  })
  test("live reachable", async () => {
    await expectRoute("/live", /实盘/)
  })
  test("risk reachable", async () => {
    await expectRoute("/risk", /Risk|风控/)
  })
  test("nav has 5 links", async () => {
    render(
      <MemoryRouter initialEntries={["/dashboard"]}>
        <App />
      </MemoryRouter>
    )
    // nav links: expect 5 distinct destinations
    const links = await screen.findAllByRole("link")
    const hrefs = links.map((a) => (a as HTMLAnchorElement).getAttribute("href") || "")
    // at least 5 routes present
    expect(hrefs).toEqual(expect.arrayContaining(["/dashboard", "/research", "/backtest", "/live", "/risk"]))
    expect(hrefs.length).toBeGreaterThanOrEqual(5)
  })
})
