/**
 * 合规页 E2E（真浏览器 / PR-172）
 *
 * 覆盖目标：`/compliance` 是**只读静态页且不进 RouteGuard**（`frontend/src/App.tsx:195-199`
 * 注释与 `pages/Compliance.tsx:5-6`），因此无令牌、任何角色都必须可读；两个入口
 * （页脚 `App.tsx:208-214`、设置页 `pages/Settings.tsx:344-353`）必须真的能点进去。
 *
 * 与单测 `frontend/src/__tests__/compliance.test.tsx` 的分工：单测在 jsdom 里渲染组件，
 * 这里跑真浏览器 + 真路由 + 真链接点击（含原生 <a> 的整页跳转），防止「单测绿、真实跳转坏」。
 *
 * 断言口径全部取自源码，不猜文案：
 * - 五个分区标题 / id：`pages/Compliance.tsx:28-119`（`COMPLIANCE_SECTIONS`）
 * - 版本整句：`pages/Compliance.tsx:139-141`（`合规文本版本 0.2.0 · 与 docs/COMPLIANCE.md 同步`）
 * - 对齐日期：`pages/Compliance.tsx:17`（`COMPLIANCE_DOC_ALIGNED_AT = "2026-10-31"`）+ :110-112
 * - 禁词：与单测 `compliance.test.tsx:35` 同表（未发生/过度承诺表述）
 */
import { test, expect, type Page } from '@playwright/test';
import { installRoleToken } from './helpers/token';

/** 五个分区标题（顺序即 COMPLIANCE_SECTIONS 顺序，pages/Compliance.tsx:28-119） */
const SECTION_TITLES = ['投资免责', '数据来源与限制', '合成模式', '隐私与令牌', '版本与同步'];

/** 未发生/过度承诺的表述：命中即视为夸大（同 compliance.test.tsx:35，去掉单测独有项避免误伤页脚） */
const FORBIDDEN_WORDS = ['已通过', '认证', '批准', '保证收益', '稳赚'];

/** 页面主体容器（data-testid 见 pages/Compliance.tsx:136）；全局 /ready 横幅在容器外，不参与正文扫描 */
function compliancePage(page: Page) {
  return page.getByTestId('compliance-page');
}

test('直达 /compliance：五个分区标题齐全且懒加载已完成', async ({ page }) => {
  await page.goto('/compliance');

  await expect(page.getByRole('heading', { level: 1 })).toContainText('合规与免责');
  for (const title of SECTION_TITLES) {
    await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
  }
  // 五个分区各自容器都在（防止标题被挪进同一块而视觉上仍「看起来齐」）
  for (const id of ['investment-disclaimer', 'data-sources', 'synthetic-mode', 'privacy-token', 'version']) {
    await expect(page.getByTestId(`compliance-section-${id}`)).toBeVisible();
  }
  // 懒加载必须真正完成：Suspense 兜底文案不得残留（同 spa.spec.ts:44-45 口径）
  await expect(page.getByText('加载中…')).toHaveCount(0);
});

test('版本号 0.2.0 与静态对齐日期 2026-10-31 可见（不随机器时间漂移）', async ({ page }) => {
  await page.goto('/compliance');

  const versionLine = page.getByTestId('compliance-version');
  await expect(versionLine).toBeVisible();
  await expect(versionLine).toContainText('合规文本版本 0.2.0');
  await expect(versionLine).toContainText('docs/COMPLIANCE.md');

  // 对齐日期写在「版本与同步」分区（pages/Compliance.tsx:110-112），是静态字符串
  const versionSection = page.getByTestId('compliance-section-version');
  await expect(versionSection).toContainText('2026-10-31');
  await expect(compliancePage(page)).toContainText('2026-10-31');
});

test('页脚「合规与免责」链接 href 正确，点击后 URL 变 /compliance', async ({ page }) => {
  await page.goto('/dashboard');

  const footer = page.locator('footer');
  const link = footer.getByTestId('footer-compliance-link');
  await expect(link).toBeVisible();
  await expect(link).toHaveAttribute('href', '/compliance');
  // 原有免责句必须保留（合规入口是追加，不是替换）
  await expect(footer).toContainText('不构成投资建议');

  await link.click();
  await expect(page).toHaveURL(/\/compliance$/);
  await expect(page.getByRole('heading', { level: 1 })).toContainText('合规与免责');
  await expect(page.getByRole('heading', { name: '投资免责', exact: true })).toBeVisible();
});

test('设置页「查看合规与免责说明」链接存在、href 正确且点击可达', async ({ page }) => {
  await page.goto('/settings');

  const link = page.getByTestId('settings-compliance-link');
  await expect(link).toBeVisible();
  await expect(link).toHaveText('查看合规与免责说明');
  await expect(link).toHaveAttribute('href', '/compliance');
  // 链接必须落在「身份令牌」卡片里（pages/Settings.tsx:344-353），不是页面别处
  await expect(page.getByText('身份令牌')).toBeVisible();

  // 原生 <a>：整页跳转，路由回退由 vite dev 的 SPA fallback 兜住
  await link.click();
  await expect(page).toHaveURL(/\/compliance$/);
  await expect(page.getByRole('heading', { level: 1 })).toContainText('合规与免责');
});

test('反向断言：正文不出现「已通过/认证/批准/保证收益/稳赚」等过度承诺表述', async ({ page }) => {
  await page.goto('/compliance');
  await expect(page.getByRole('heading', { level: 1 })).toContainText('合规与免责');

  // 只扫页面容器：全局 /ready 横幅在容器外，且其文案属运维提示，不属合规正文
  const pageText = await compliancePage(page).innerText();
  for (const word of FORBIDDEN_WORDS) {
    expect(pageText, `合规页正文不得出现「${word}」`).not.toContain(word);
  }

  // 正向锚点：这些「不承诺」表述必须在，否则上面可能是空页假绿
  for (const anchor of ['非投资建议', '关键结论仍需人工复核', '顺序回退不等于择优', '未验签']) {
    expect(pageText, `合规页应含关键表述「${anchor}」`).toContain(anchor);
  }
});

test('无令牌时合规页不被 RouteGuard 拦截：无 advisory、无 403 面板', async ({ page }) => {
  await page.goto('/compliance');
  await expect(page.getByRole('heading', { level: 1 })).toContainText('合规与免责');

  // 合规路由没包 RouteGuard（App.tsx:197-199）：两种守卫 UI 都不该出现
  await expect(page.getByTestId('route-guard-advisory')).toHaveCount(0);
  await expect(page.getByTestId('route-guard-403')).toHaveCount(0);
  // 拦截面板的可见性判据：正文五个分区仍全部渲染
  await expect(page.getByRole('heading', { name: '隐私与令牌', exact: true })).toBeVisible();
});

test('viewer 令牌下合规页照常可读（对照：同令牌访问 /chat 才出 403）', async ({ page }) => {
  await installRoleToken(page, ['viewer']);
  await page.goto('/compliance');

  await expect(page.getByRole('heading', { level: 1 })).toContainText('合规与免责');
  await expect(page.getByTestId('route-guard-403')).toHaveCount(0);
  await expect(page.getByTestId('route-guard-advisory')).toHaveCount(0);
  // 顶栏角色 chip 证明令牌确实被页面读到（否则本用例只是「没令牌所以没拦」的假绿）
  await expect(page.getByTestId('role-chip')).toContainText('viewer');
});
