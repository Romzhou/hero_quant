/// <reference types="vitest" />
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import type { IncomingMessage } from "node:http"

const _apiTarget = process.env.VITE_API_TARGET || "http://localhost:8899"

export default defineConfig({
  plugins: [react()],
  optimizeDeps: { include: ["echarts", "echarts-for-react"] },
  build: { chunkSizeWarningLimit: 1200 },
  server: {
    host: "0.0.0.0",
    proxy: {
      // /live 同时是 SPA 路由与后端探活接口：文档导航必须交回 index.html，
      // 否则 dev 下直接打开 /live 会被代理到后端（后端缺席即 500）
      "/live": {
        target: _apiTarget,
        bypass: (req: IncomingMessage) =>
          String(req.headers.accept || "").includes("text/html") ? "/index.html" : undefined
      },
      "/ready": _apiTarget,
      "/metrics": _apiTarget,
      "/v1": _apiTarget
    }
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/__tests__/setup.ts"]
  }
})
