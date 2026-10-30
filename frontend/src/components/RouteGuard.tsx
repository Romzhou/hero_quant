/**
 * RouteGuard —— 前端角色守卫（PR-103）
 *
 * 口径（务必与 `config/auth.ts:1-8` 保持一致，不得推翻）：
 * - 前端**只解 JWT payload 做展示与 UX 守卫**，不验签、不裁决权限；
 *   真正的 401/403 一律由后端（`auth` 中间件 / `ensure_role`）返回。
 * - 前端无法得知后端是否开启鉴权（`/ready` 不返回 auth 模式），因此：
 *   · 无令牌 / 令牌无 roles 声明 → 只给 advisory 提示，**不拦截渲染**（避免把未开鉴权的开发环境锁死）；
 *   · 有令牌且 roles 足以判定不足 → 拦截渲染且不发请求（后端若已开鉴权，省掉一次注定 403 的往返）。
 * - 所有面板文案都带「未验签 / 以服务端为准」，禁止让用户误以为是安全边界。
 */
import { useEffect, useMemo, useState, type ReactNode } from "react"
import { Link } from "react-router-dom"
import {
  TOKEN_CHANGED_EVENT,
  getToken,
  hasRole,
  inspectToken,
  roleLabel,
  type Role,
  type TokenInfo,
} from "../config/auth"

/**
 * 读取当前令牌声明并在令牌变更后自动刷新：
 * - 同标签页：Settings 保存/清除后 dispatch `hero-quant:token-changed`（localStorage 不会自发事件）；
 * - 跨标签页：监听原生 `storage` 事件。
 * 不引入全局状态库：角色只是令牌的派生读数，重新解一遍 payload 即可。
 */
export function useTokenInfo(): TokenInfo {
  const [token, setToken] = useState<string | null>(() => getToken())
  useEffect(() => {
    const sync = () => setToken(getToken())
    window.addEventListener("storage", sync)
    window.addEventListener(TOKEN_CHANGED_EVENT, sync)
    return () => {
      window.removeEventListener("storage", sync)
      window.removeEventListener(TOKEN_CHANGED_EVENT, sync)
    }
  }, [])
  return useMemo(() => inspectToken(token), [token])
}

const ADVISORY_CLS =
  "rounded-xl border border-amber-500/25 bg-amber-500/10 px-3 py-2 text-xs leading-5 text-amber-200"
// 与 App.tsx 的 DegradedBanner 外层同宽同 padding，避免 advisory 与页面内容错位
const ADVISORY_WRAP_CLS = "mx-auto max-w-7xl px-4 pt-4 md:px-6"
const PANEL_CLS = "mx-auto max-w-3xl px-6 py-10"

/** advisory 提示条：改变「预期」但不改变「是否渲染」 */
export function RoleAdvisory({ children }: { children: ReactNode }) {
  return (
    <div role="status" data-testid="route-guard-advisory" className={ADVISORY_CLS}>
      {children}
    </div>
  )
}

/** 403 面板：仅在「令牌明确声明了角色且不满足」时出现 */
function RoleBlocked({ required, title, declared }: { required: Role; title?: string; declared: Role[] }) {
  return (
    <div data-testid="route-guard-403" className={PANEL_CLS}>
      <div className="rounded-2xl border border-red-400/20 bg-red-400/[0.07] p-5">
        <h2 className="text-sm font-semibold text-red-200">
          {title ? `${title} · ` : ""}权限不足（403）
        </h2>
        <p className="mt-2 text-sm leading-6 text-slate-300">
          需要 <span className="font-mono text-mist">{required}</span>（{roleLabel(required)}）及以上；当前令牌声明：
          <span data-testid="route-guard-declared" className="font-mono text-mist">
            {declared.length ? declared.join(", ") : "（无）"}
          </span>
        </p>
        <p className="mt-2 text-xs leading-5 text-slate-400">
          本页未发起请求——前端按令牌声明判定权限不足；若后端已开启鉴权，该请求会被 403 拒绝。
          角色来自令牌声明，<span className="text-slate-300">未验签、未由服务端确认</span>，实际权限以服务端裁决为准。
        </p>
        <Link
          to="/settings"
          className="mt-4 inline-block rounded-lg border border-amber-500/30 bg-amber-500/15 px-3 py-1.5 text-xs text-amber-200 hover:bg-amber-500/25"
        >
          去设置页更换令牌
        </Link>
      </div>
    </div>
  )
}

export interface RouteGuardProps {
  /** 该路由需要的最低角色（后端端点口径：/v1/query*=analyst，/v1/backtest/*、/v1/sessions*=viewer） */
  required: Role
  /** 展示用页面名（403 面板标题） */
  title?: string
  children: ReactNode
}

export default function RouteGuard({ required, title, children }: RouteGuardProps) {
  const info = useTokenInfo()
  const token = getToken()

  // 1) 无令牌：前端不知道后端是否开鉴权，硬拦会锁死未开鉴权的开发环境 → advisory 放行
  if (!token) {
    return (
      <>
        <div className={ADVISORY_WRAP_CLS}>
          <RoleAdvisory>
            未提供令牌：若后端已开启鉴权，本页请求将被 401/403；令牌入口在
            <Link to="/settings" className="mx-1 underline decoration-dotted">设置页</Link>
            。前端仅解令牌做提示，未验签。
          </RoleAdvisory>
        </div>
        {children}
      </>
    )
  }

  // 2) 有令牌但未声明角色：前端无从判断，advisory 放行（不猜默认角色）
  if (info.roles.length === 0) {
    return (
      <>
        <div className={ADVISORY_WRAP_CLS}>
          <RoleAdvisory>
            令牌未声明角色，权限以服务端为准（前端不做裁决{info.error ? `；令牌解析：${info.error}` : ""}）。
          </RoleAdvisory>
        </div>
        {children}
      </>
    )
  }

  // 3) 角色明确不足：拦截渲染，不发请求
  if (!hasRole(info, required)) {
    return <RoleBlocked required={required} title={title} declared={info.roles} />
  }

  // 4) 满足：正常渲染
  return <>{children}</>
}
