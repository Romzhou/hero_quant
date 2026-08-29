import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"

const _apiTarget = process.env.VITE_API_TARGET || "http://localhost:8899"

export default defineConfig({
  plugins: [react()],
  optimizeDeps: { include: ["echarts", "echarts-for-react"] },
  build: { chunkSizeWarningLimit: 1200 },
  server: {
    host: "0.0.0.0",
    proxy: {
      "/live": _apiTarget,
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
