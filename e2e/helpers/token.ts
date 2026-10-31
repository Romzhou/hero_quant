/**
 * e2e 令牌构造 / 注入 helper（PR-103 角色守卫 + PR-172 合规页共用）
 *
 * 依据：`frontend/src/config/auth.ts:145-193`（`_b64urlDecode` + `inspectToken`）只解 JWT
 * 第二段的 base64url payload，**不验签**（同文件 :1-8 注释：前端不裁决权限）。
 * 因此 e2e 只需造「三段式 + payload 含 roles」的假令牌，第三段随便填即可，
 * 与单测 `frontend/src/__tests__/roleGuard.test.tsx` 的 makeJwt 同构。
 *
 * 注入点：`config/auth.ts:11` 的 `TOKEN_STORAGE_KEY = "hero-quant.token"`，必须用
 * `page.addInitScript` 在页面脚本执行前写入，否则 React 首帧读不到令牌（useTokenInfo
 * 的初始 state 是 `getToken()`，见 `components/RouteGuard.tsx:31`）。
 */
import type { Page } from '@playwright/test';

/** 与 frontend/src/config/auth.ts:11 一致；改这里必须同步改源码，否则注入静默失效 */
export const TOKEN_STORAGE_KEY = 'hero-quant.token';

/** 与后端 `auth.principal.Role` / `config/auth.ts:71` 的白名单一致 */
export type Role = 'viewer' | 'analyst' | 'admin';

/** base64url 编码（Node 侧；payload 一律 UTF-8，前端用 TextDecoder 还原） */
export function b64urlJson(value: unknown): string {
  return Buffer.from(JSON.stringify(value), 'utf8').toString('base64url');
}

export interface MakeTokenOptions {
  sub?: string;
  /** 与 auth.ts:180-182 的 tenant_id / tenant 双键口径对齐：只给一个，避免「不一致」判定 */
  tenantId?: string;
  /** 有效期（秒）。前端只展示 expired，不做拦截，默认给足 1 小时避免时间漂移 */
  ttlSeconds?: number;
  /** 追加/覆盖声明（例如 realm_access 回退路径） */
  extraClaims?: Record<string, unknown>;
}

/**
 * 造三段式假 JWT：header.payload.sig。
 * 签名段固定 "sig" —— 前端不验签（auth.ts:162-165 注释），后端在 e2e 环境未开启鉴权。
 */
export function makeToken(roles: Role[], options: MakeTokenOptions = {}): string {
  const { sub = 'e2e-user', tenantId = 'tenant-e2e', ttlSeconds = 3600, extraClaims = {} } = options;
  const now = Math.floor(Date.now() / 1000);
  const header = b64urlJson({ alg: 'none', typ: 'JWT' });
  const payload = b64urlJson({
    sub,
    tenant_id: tenantId,
    roles,
    iat: now,
    exp: now + ttlSeconds,
    ...extraClaims,
  });
  return `${header}.${payload}.sig`;
}

/**
 * 在页面加载前把令牌写入 localStorage（`page.addInitScript` 在每次导航的 document
 * 脚本执行前运行）。隐私模式下 localStorage 抛错时忽略：e2e 断言会因此失败并被看到，
 * 不会假绿。
 */
export async function installToken(page: Page, token: string): Promise<void> {
  await page.addInitScript(
    ({ key, value }: { key: string; value: string }) => {
      try {
        window.localStorage.setItem(key, value);
      } catch {
        /* 隐私模式：令牌装不进去，后续断言自然失败，不掩盖 */
      }
    },
    { key: TOKEN_STORAGE_KEY, value: token },
  );
}

/** 便捷组合：按角色造令牌并注入 */
export async function installRoleToken(
  page: Page,
  roles: Role[],
  options: MakeTokenOptions = {},
): Promise<string> {
  const token = makeToken(roles, options);
  await installToken(page, token);
  return token;
}
