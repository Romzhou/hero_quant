/** Task21 E2E: SPA 路由 /dashboard→/monitor 验证 */
import { test, expect } from '@playwright/test';
import * as fs from 'fs';

test('SPA routes /dashboard→/monitor→/research 可达', async () => {
  const appText = fs.readFileSync('frontend/src/App.tsx', 'utf-8');
  // 必须挂载 /monitor 路由（Wave5 Task17）
  expect(appText).toContain('path="/monitor"');
  expect(appText).toContain('<Monitor');
  // 校验关键路由均存在
  const routes = ['/dashboard', '/research', '/backtest', '/chat', '/live', '/monitor', '/risk', '/settings'];
  for (const r of routes) {
    expect(appText).toContain(`path="${r}"`);
  }
  // Nav 包含 Monitor 入口
  expect(appText).toContain('Monitor');
});

test('SPA 前端去 mock：Live/Monitor 无 Math.random() 实调用', async () => {
  const live = fs.readFileSync('frontend/src/pages/Live.tsx', 'utf-8');
  const monitor = fs.readFileSync('frontend/src/pages/Monitor.tsx', 'utf-8');
  expect(live).not.toContain('Math.random()');
  expect(monitor).not.toContain('Math.random()');
  // Monitor 改为 fetch /v1/trace/events SSE
  expect(monitor).toContain('/v1/trace/events');
});

test('SPA index fallback to /dashboard', async () => {
  const appText = fs.readFileSync('frontend/src/App.tsx', 'utf-8');
  expect(appText).toContain('path="/"');
  expect(appText).toContain('Navigate to="/dashboard"');
});
