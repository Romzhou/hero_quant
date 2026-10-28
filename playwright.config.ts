import { defineConfig } from '@playwright/test';

/**
 * Playwright 配置（E线）
 * - 真浏览器：复用宿主 /usr/bin/google-chrome（无 ~/.cache/ms-playwright，禁止 npx playwright install）
 * - webServer：起 vite dev（proxy /v1 → localhost:8899）。后端缺席时各 spec 断言错误/空态，不冒充成功
 * - 4 个 project 全部真调度，无 test.skip
 */
export default defineConfig({
  testDir: 'e2e',
  timeout: 45_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  reporter: 'list',
  use: {
    baseURL: 'http://127.0.0.1:5173',
    trace: 'off',
    launchOptions: {
      // 系统 Chrome：CHROME_PATH 可覆盖；root 下必须 --no-sandbox
      executablePath: process.env.CHROME_PATH || '/usr/bin/google-chrome',
      args: ['--no-sandbox', '--disable-dev-shm-usage'],
    },
  },
  webServer: {
    command: 'npm run dev -- --strictPort',
    cwd: 'frontend',
    url: 'http://127.0.0.1:5173',
    reuseExistingServer: true,
    timeout: 120_000,
  },
  projects: [
    {
      name: 'chat-sse',
      testMatch: /chat\.spec\.ts/,
    },
    {
      name: 'research-echarts',
      testMatch: /research\.spec\.ts/,
    },
    {
      name: 'spa-routes',
      testMatch: /spa\.spec\.ts/,
    },
    {
      name: 'reconciliation',
      testMatch: /reconciliation\.spec\.ts/,
    },
  ],
});
