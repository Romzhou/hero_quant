import { defineConfig } from '@playwright/test';
import { existsSync } from 'node:fs';

/**
 * Playwright 配置（E线）
 * - 真浏览器：复用宿主 /usr/bin/google-chrome（无 ~/.cache/ms-playwright，禁止 npx playwright install）
 * - webServer：起 vite dev（proxy /v1 → localhost:8899）。后端缺席时各 spec 断言错误/空态，不冒充成功
 * - 4 个 project 全部真调度，无 test.skip
 */
function chromePath(): string | undefined {
  const candidates = [process.env.CHROME_PATH, '/usr/bin/google-chrome', '/opt/google/chrome/chrome'];
  return candidates.find(p => !!p && existsSync(p));
}

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
      // 系统 Chrome：CHROME_PATH 优先，其次 /usr/bin/google-chrome；都没有则不指定（交给 channel/内置解析）
      // 不下载 Playwright 浏览器（本地无缓存、CI 用 runner 预装 Chrome）
      executablePath: chromePath(),
      args: ['--no-sandbox', '--disable-dev-shm-usage'],
    },
  },
  webServer: {
    command: 'npm run dev -- --strictPort',
    cwd: 'frontend',
    url: 'http://127.0.0.1:5173',
    // 本地复用已起的 dev server；CI 由自己拉起（端口不应被占）
    reuseExistingServer: !process.env.CI,
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
