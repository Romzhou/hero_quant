/**
 * Chat SSE E2E（真浏览器）
 * - 真实 page.goto + 输入 + 点击发送 + DOM 断言，替代原先 spawn python 读源码的假测试
 * - 后端缺席/降级：断言 degraded 横幅（不把合成应答当成功）；有工具事件时断言真实 latency 上屏
 * - 事件源两条链路都覆盖：EventSource 走 ?ticket=（后端 403，预期）→ fetch 回退走 X-Ticket 头
 */
import { test, expect, type Page } from '@playwright/test';

const DEGRADED_FRAME = { degraded: true, reason: 'missing HERO_API_KEY, synthetic placeholder response' };

function sseBody(frames: unknown[]): string {
  return frames.map(f => `data: ${f === '[DONE]' ? '[DONE]' : JSON.stringify(f)}\n\n`).join('');
}

async function stubSse(page: Page, frames: unknown[]) {
  // 票据：POST /v1/query/ticket
  await page.route('**/v1/query/ticket', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ticket: 'e2e-ticket', expires_in: 60 }) })
  );
  await page.route('**/v1/query/stream**', route => {
    const hasHeaderTicket = Boolean(route.request().headers()['x-ticket']);
    if (!hasHeaderTicket) {
      // EventSource 不支持自定义 header，只带 ?ticket=：后端只认 X-Ticket，403 属预期，触发 fetch 回退
      return route.fulfill({ status: 403, body: 'ticket must be header' });
    }
    return route.fulfill({
      status: 200,
      headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-store' },
      body: sseBody(frames),
    });
  });
}

test('空会话显示「暂无工具调用」，不出现写死的 42ms/180ms', async ({ page }) => {
  await page.goto('/chat');
  await expect(page.getByRole('heading', { level: 1 })).toContainText('对话 · 投研对话');
  await expect(page.getByText('暂无工具调用')).toBeVisible();
  const body = await page.locator('body').innerText();
  expect(body).not.toContain('42ms');
  expect(body).not.toContain('180ms');
});

test('发送问题 → 显示后端 degraded 横幅，而不是假装成功', async ({ page }) => {
  await stubSse(page, [DEGRADED_FRAME, { type: 'delta', delta: '这是未配置 HERO_API_KEY 时的占位应答' }, '[DONE]']);
  await page.goto('/chat');
  await page.getByPlaceholder(/输入投研问题/).fill('回测 600519.SH 近一月等权');
  await page.getByRole('button', { name: '发送' }).click();

  const banner = page.getByTestId('degraded-banner');
  await expect(banner).toBeVisible();
  await expect(banner).toContainText('synthetic placeholder response');
  await expect(banner).toContainText('Chat SSE');
  // 用户消息已上屏（快捷问句按钮里也有同名文案，取第一个可见气泡）
  await expect(page.getByText('回测 600519.SH 近一月等权').first()).toBeVisible();
});

test('后端推送 type=tool 时按真实 latency 渲染轨迹', async ({ page }) => {
  await stubSse(page, [
    { type: 'tool', tool: 'get_market_data', status: 'success', latencyMs: 77, preview: '600519.SH 天勤日线' },
    { type: 'delta', delta: '600519.SH 近一月等权回测完成' },
    '[DONE]',
  ]);
  await page.goto('/chat');
  await page.getByPlaceholder(/输入投研问题/).fill('分析 600519.SH RSI 是否超买');
  await page.getByRole('button', { name: '发送' }).click();

  await expect(page.getByText('get_market_data')).toBeVisible();
  await expect(page.getByText('77ms')).toBeVisible();
  await expect(page.getByText('600519.SH 近一月等权回测完成')).toBeVisible();
  await expect(page.getByTestId('degraded-banner')).toHaveCount(0);
});

test('票据接口 500 时显示错误态，不展示成功气泡', async ({ page }) => {
  await page.route('**/v1/query/ticket', route => route.fulfill({ status: 500, body: 'boom' }));
  await page.goto('/chat');
  await page.getByPlaceholder(/输入投研问题/).fill('回测 600519.SH');
  await page.getByRole('button', { name: '发送' }).click();
  await expect(page.getByText(/HTTP 500/).first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('degraded-banner')).toHaveCount(0);
});
