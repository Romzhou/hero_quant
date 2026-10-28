/**
 * SPA 路由 E2E（真浏览器）
 * - 真 goto + DOM 断言，替代原先 read_text() 读源码字符串的假测试
 * - /monitor 的 SSE 一律 abort：无真实事件时必须保留「演示数据」标记（不冒充实时）
 */
import { test, expect } from '@playwright/test';

test('/ 重定向到 /dashboard 且主导航可见', async ({ page }) => {
  await page.goto('/');
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole('heading', { level: 1 })).toContainText('Dashboard · 总览');
  const nav = page.getByRole('navigation', { name: 'Primary' });
  await expect(nav).toBeVisible();
  for (const label of ['看板', '研究', '回测', '实盘', '监控', '风控']) {
    await expect(nav.getByRole('link', { name: label })).toBeVisible();
  }
});

test('导航点击 看板 → 监控：URL 变化且监控页渲染 + 保留演示标记', async ({ page }) => {
  // 断开 SSE：无真实 trace 事件，页面不得自行摘掉「演示数据」
  await page.route('**/v1/trace/events**', route => route.abort());
  await page.goto('/dashboard');
  await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: '监控' }).click();
  await expect(page).toHaveURL(/\/monitor$/);
  await expect(page.getByRole('heading', { level: 1 })).toContainText('Live 监控 · 运行态');
  await expect(page.getByText('演示数据')).toBeVisible();
});

const routes: Array<{ path: string; heading: string }> = [
  { path: '/research', heading: '投研 · 研究' },
  { path: '/backtest', heading: '对话 · 投研对话' },
  { path: '/chat', heading: '对话 · 投研对话' },
  { path: '/live', heading: 'Live 监控 · 实盘' },
  { path: '/monitor', heading: 'Live 监控 · 运行态' },
  { path: '/risk', heading: 'Risk · 风控' },
  { path: '/settings', heading: '设置' },
];

for (const r of routes) {
  test(`直达 ${r.path} 渲染 ${r.heading}`, async ({ page }) => {
    await page.route('**/v1/trace/events**', route => route.abort());
    await page.goto(r.path);
    await expect(page.getByRole('heading', { level: 1 })).toContainText(r.heading);
    // 懒加载必须真正完成：Suspense 兜底文案不得残留
    await expect(page.getByText('加载中…')).toHaveCount(0);
  });
}
