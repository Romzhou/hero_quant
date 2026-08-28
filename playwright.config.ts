import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: 'e2e',
  timeout: 30_000,
  fullyParallel: true,
  reporter: 'list',
  use: {
    baseURL: 'http://localhost:5173',
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
  ],
});
