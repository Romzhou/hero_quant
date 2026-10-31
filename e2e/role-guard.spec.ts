/**
 * 角色守卫 E2E（真浏览器 / PR-103）
 *
 * 断言对象是**纯前端 UX 守卫**：`components/RouteGuard.tsx:96-137` 的四态
 *   1) 无令牌 → advisory 放行（:100-114）
 *   2) 有令牌但无 roles 声明 → advisory 放行（:116-128）
 *   3) 令牌声明角色且不足 → 403 面板、不渲染子页面（:130-133）
 *   4) 满足 → 正常渲染（:135-136）
 * 因此本 spec **不依赖后端鉴权**：e2e 环境 `HERO_ENV=development` 下后端可能未开 auth，
 * 若断言后端 401/403 会随环境漂移；这里只断言 UI 状态。
 *
 * 令牌：`helpers/token.ts` 造三段式假 JWT 并 `addInitScript` 注入 localStorage
 * （键见 `config/auth.ts:11`）。前端不验签（`config/auth.ts:162-165`），签名段随便填。
 *
 * 文案/属性全部取自源码：
 * - 403 标题：`RouteGuard.tsx:64-66`（`${title} · 权限不足（403）`）
 * - 已声明角色：`RouteGuard.tsx:69-71`（data-testid=route-guard-declared）
 * - 自救链接：`RouteGuard.tsx:77-82`（去设置页更换令牌 → /settings）
 * - advisory：`RouteGuard.tsx:51-57`（data-testid=route-guard-advisory，无令牌文案见 :106-107）
 * - 路由所需角色：`App.tsx:187-194`（/chat = analyst，/dashboard = viewer）
 */
import { test, expect, type Page } from '@playwright/test';
import { installRoleToken } from './helpers/token';

/** 守卫面板定位器（RouteGuard.tsx:62 / :53） */
const panel403 = (page: Page) => page.getByTestId('route-guard-403');
const advisory = (page: Page) => page.getByTestId('route-guard-advisory');

/** Chat 页输入框（pages/Chat.tsx:694-706，无 testid，用 placeholder 定位，同 chat.spec.ts:51） */
const chatInput = (page: Page) => page.getByPlaceholder(/输入投研问题/);

test('viewer 令牌访问 /chat：出 403 面板、声明角色可见、且不渲染 Chat 输入框', async ({ page }) => {
  await installRoleToken(page, ['viewer']);
  await page.goto('/chat');

  await expect(panel403(page)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole('heading', { name: /权限不足（403）/ })).toBeVisible();
  // 面板必须点名「需要 analyst」并回显令牌声明角色，避免用户不知道该换什么令牌
  await expect(panel403(page)).toContainText('analyst');
  await expect(page.getByTestId('route-guard-declared')).toHaveText('viewer');
  // 拦截的是渲染，不是文案：Chat 主体（输入框 / 标题 / 发送按钮）都不该出现
  await expect(chatInput(page)).toHaveCount(0);
  await expect(page.getByRole('button', { name: '发送' })).toHaveCount(0);
  await expect(page.getByRole('heading', { level: 1 })).toHaveCount(0);
  // 口径声明必须在：前端未验签、不构成安全边界（RouteGuard.tsx:73-76）
  await expect(panel403(page)).toContainText('未验签');
  await expect(panel403(page)).toContainText('本页未发起请求');
});

test('viewer 令牌的 403 面板提供自救路径：点击「去设置页更换令牌」到 /settings', async ({ page }) => {
  await installRoleToken(page, ['viewer']);
  await page.goto('/chat');
  await expect(panel403(page)).toBeVisible({ timeout: 15_000 });

  const rescue = panel403(page).getByRole('link', { name: '去设置页更换令牌' });
  await expect(rescue).toBeVisible();
  await rescue.click();

  // 设置页不设角色闸（App.tsx:195-196），低权令牌必须能进来换令牌
  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByRole('heading', { level: 1 })).toContainText('设置');
  await expect(panel403(page)).toHaveCount(0);
});

test('analyst 令牌访问 /chat：正常渲染，无 403 也无 advisory', async ({ page }) => {
  await installRoleToken(page, ['analyst']);
  await page.goto('/chat');

  await expect(page.getByRole('heading', { level: 1 })).toContainText('对话 · 投研对话', { timeout: 15_000 });
  await expect(chatInput(page)).toBeVisible();
  await expect(page.getByRole('button', { name: '发送' })).toBeVisible();
  await expect(panel403(page)).toHaveCount(0);
  // 有令牌且声明了满足要求的角色 → 不该再出 advisory（RouteGuard.tsx:135-136）
  await expect(advisory(page)).toHaveCount(0);
  // 顶栏 chip 证明令牌确实生效（text 见 App.tsx:53）
  await expect(page.getByTestId('role-chip')).toContainText('analyst');
});

test('admin 令牌访问 /chat：等级包含，正常渲染', async ({ page }) => {
  await installRoleToken(page, ['admin']);
  await page.goto('/chat');

  // admin 满足 analyst（ROLE_RANK 单向包含，config/auth.ts:126-134）
  await expect(page.getByRole('heading', { level: 1 })).toContainText('对话 · 投研对话', { timeout: 15_000 });
  await expect(chatInput(page)).toBeVisible();
  await expect(panel403(page)).toHaveCount(0);
  await expect(page.getByTestId('role-chip')).toContainText('admin');
});

test('无令牌访问 /chat：advisory 提示但不拦截渲染', async ({ page }) => {
  await page.goto('/chat');

  // 后端是否开鉴权前端无从得知 → 只提示不硬拦（RouteGuard.tsx:100-114）
  await expect(advisory(page)).toBeVisible({ timeout: 15_000 });
  await expect(advisory(page)).toContainText('未提供令牌');
  await expect(advisory(page)).toContainText('未验签');
  await expect(panel403(page)).toHaveCount(0);
  // advisory 不改变「是否渲染」：Chat 主体照常上屏
  await expect(page.getByRole('heading', { level: 1 })).toContainText('对话 · 投研对话');
  await expect(chatInput(page)).toBeVisible();
});

test('viewer 令牌访问 /dashboard：viewer 已达标，不被拦', async ({ page }) => {
  await installRoleToken(page, ['viewer']);
  await page.goto('/dashboard');

  await expect(page.getByRole('heading', { level: 1 })).toContainText('Dashboard · 总览', { timeout: 15_000 });
  await expect(panel403(page)).toHaveCount(0);
  await expect(advisory(page)).toHaveCount(0);
  // 导航锁定与路由守卫同源：viewer 看「回测」被标 locked，「看板」不标（App.tsx:120-124,142-163）
  const nav = page.getByRole('navigation', { name: 'Primary' });
  await expect(nav.getByRole('link', { name: '回测' })).toHaveAttribute('data-locked', 'true');
  await expect(nav.getByRole('link', { name: '看板' })).not.toHaveAttribute('data-locked', 'true');
});
