/// <reference types="vite/client" />
/**
 * 集中 API 配置（前端）
 * - 职责：统一 API 基路径与后端产物路径，base-path/网关变更只需改一处，避免跨页硬编码漂移
 * - 基路径取自 Vite `import.meta.env.BASE_URL`（默认 "/"），子路径部署自动带前缀
 */

export const API_BASE: string = import.meta.env.BASE_URL ?? "/"

function withBase(path: string): string {
  const base = API_BASE.endsWith("/") ? API_BASE.slice(0, -1) : API_BASE
  return `${base}${path}`
}

// 后端回测产物与探活路径
export const API_METRICS = withBase("/v1/backtest/metrics.json")
export const API_POSITIONS = withBase("/v1/backtest/positions.csv")
export const API_TEARSHEET = withBase("/v1/backtest/tearsheet.html")
export const API_DRAWDOWNS = withBase("/v1/backtest/drawdowns.json")
export const API_LIVE = withBase("/live")

// 裸路径（无 BASE 前缀）：远程 apiBase 直连网关时使用，避免把子路径部署前缀带到远端造成 404
export const RAW_API_PATHS = {
  LIVE: "/live",
  METRICS: "/v1/backtest/metrics.json",
} as const

// 前端路由集中管理，避免各页硬编码字符串漂移
export const ROUTES = {
  DASHBOARD: "/dashboard",
  RESEARCH: "/research",
  BACKTEST: "/backtest",
  LIVE: "/live",
  RISK: "/risk",
  SETTINGS: "/settings",
} as const
