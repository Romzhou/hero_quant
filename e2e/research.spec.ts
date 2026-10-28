/**
 * Research / 回测产物 E2E（真浏览器 + 真 DOM）
 * - 覆盖真实引擎 schema：positions.csv = date,asset_0,asset_1（无 close 列）→ 净值区必须诚实空态
 * - metrics.json 带 synthetic/degraded/reason 标记时必须原样上屏，不得伪装成真实回测
 * - 无后端（全部 abort）时断言降级/错误态，绝不断言成功态
 */
import { test, expect } from '@playwright/test';

const METRICS = {
  sharpe: 1.23,
  annual_return: 0.11,
  max_drawdown: -0.045,
  turnover: 0.31,
  monthly_returns: { '2026-09': 0.021, '2026-10': -0.013 },
  synthetic: true,
  degraded: true,
  reason: 'backtest:synthetic_demo',
};

const POSITIONS_CSV = [
  'date,asset_0,asset_1',
  '2026-10-20,0.5,0.5',
  '2026-10-21,0.6,0.4',
  '2026-10-22,0.5,0.5',
  '',
].join('\n');

test('拉取 /v1/backtest/*：指标与降级标记上屏，无 close 列时诚实空态', async ({ page }) => {
  const hits: string[] = [];
  await page.route('**/v1/backtest/**', async route => {
    const url = route.request().url();
    hits.push(url);
    if (url.includes('metrics.json')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(METRICS) });
    }
    if (url.includes('positions.csv')) {
      return route.fulfill({ status: 200, contentType: 'text/csv', body: POSITIONS_CSV });
    }
    if (url.includes('drawdowns.json')) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([{ start: '2026-10-21', end: '2026-10-22', depth: -1.4, duration: 2 }]),
      });
    }
    if (url.includes('tearsheet.html')) {
      return route.fulfill({ status: 200, contentType: 'text/html', body: '<html><body>synthetic demo tearsheet</body></html>' });
    }
    return route.fulfill({ status: 404, body: '' });
  });

  await page.goto('/research');
  await expect(page.getByRole('heading', { level: 1 })).toContainText('投研 · 研究');

  // 1) 真拉取：四类产物都请求到
  await expect.poll(() => hits.filter(h => h.includes('metrics.json')).length).toBeGreaterThan(0);
  for (const name of ['positions.csv', 'drawdowns.json', 'tearsheet.html']) {
    await expect.poll(() => hits.filter(h => h.includes(name)).length).toBeGreaterThan(0);
  }

  // 2) 指标来自后端（不是写死 1.62/18.4%）
  await expect(page.getByText('1.23')).toBeVisible();
  await expect(page.getByText('11.0%')).toBeVisible();

  // 3) 降级标记原样可见：synthetic/degraded/reason
  await expect(page.getByText(/backtest:synthetic_demo/)).toBeVisible();
  await expect(page.getByText(/degraded=true/)).toBeVisible();

  // 4) 引擎权重表无 close 列 → 诚实空态（不伪造累积净值曲线）
  await expect(page.getByText('暂无有效回测数据')).toBeVisible();
  await expect(page.getByText(/无 close 列/).first()).toBeVisible();
  await expect(page.getByText('真 positions.csv 驱动 · 含累积净值')).toHaveCount(0);

  // 5) 热力图仍由 monthly_returns 驱动渲染（ECharts 真挂载）
  await expect(page.locator('canvas').first()).toBeVisible();
});

test('无后端：展示演示/错误态而非成功态', async ({ page }) => {
  await page.route('**/v1/backtest/**', route => route.abort());
  await page.goto('/research');
  await expect(page.getByRole('heading', { level: 1 })).toContainText('投研 · 研究');

  // 拉取失败必须如实标记 isMock，不得出现「真实回测」徽标
  await expect(page.getByText(/当前指标为演示数据（合成回退，isMock=true）/)).toBeVisible();
  await expect(page.getByText('真实回测', { exact: true })).toHaveCount(0);
  await expect(page.getByText(/占位预览/)).toBeVisible();
});
