import { defineConfig } from '@playwright/test';
import { existsSync } from 'node:fs';

/**
 * Playwright 配置（E线）
 * - 浏览器：本地优先复用系统 Chrome（CHROME_PATH / /usr/bin/google-chrome）；CI 用
 *   `npx playwright install chromium` 装的自带 chromium（版本与 @playwright/test 对齐）
 * - webServer：起 vite dev（proxy /v1 → localhost:8899）。后端缺席时各 spec 断言错误/空态，不冒充成功
 * - 4 个 project 全部真调度，无 test.skip
 */
function chromePath(): string | undefined {
  const candidates = [process.env.CHROME_PATH, '/usr/bin/google-chrome', '/opt/google/chrome/chrome'];
  return candidates.find(p => !!p && existsSync(p));
}

const useBundledChromium = !!process.env.CI;

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
      // CI：不指定 executablePath/channel，走 playwright 自带 chromium（workflow 显式安装）
      // 本地：系统 Chrome 优先；都找不到才退回 channel=chrome
      executablePath: useBundledChromium ? undefined : chromePath(),
      channel: useBundledChromium || chromePath() ? undefined : 'chrome',
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
    {
      name: 'compliance',
      testMatch: /compliance\.spec\.ts/,
    },
    {
      name: 'role-guard',
      testMatch: /role-guard\.spec\.ts/,
    },
  ],
});
