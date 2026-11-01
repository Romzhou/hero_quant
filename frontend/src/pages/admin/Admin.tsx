/**
 * Admin 管理后台（PR-215）
 * - 职责：平台/租户总览、租户清单、runs 只读看板、配额用量、审计摘要、killswitch 开关
 * - 数据流：GET /v1/admin/{overview,tenants,runs,quotas,audit/summary,toggles} + POST /v1/admin/toggles；
 *   全部字段来自后端真值，缺失渲染 "--"，degraded=true 时顶部横幅展示 reason
 * - 口径：审计区禁止写「审计已启用」——只按 AuditChain.verify 的 chain_start_at / uncovered_count
 *   说明覆盖区间；非 admin 时后端 403，前端展示权限提示而不是空表
 */
import { useEffect, useState, type FormEvent } from "react"
import DegradedBanner from "../../components/DegradedBanner"
import { authHeaders } from "../../config/auth"

const API = {
  overview: "/v1/admin/overview",
  tenants: "/v1/admin/tenants?limit=50",
  runs: (status: string, limit: number) =>
    `/v1/admin/runs?limit=${limit}${status ? `&status=${encodeURIComponent(status)}` : ""}`,
  quotas: "/v1/admin/quotas",
  audit: "/v1/admin/audit/summary?limit=20",
  toggles: "/v1/admin/toggles",
}

const RUN_STATUSES = ["queued", "running", "succeeded", "failed", "cancelled", "cancelling", "dead"] as const
const TOGGLE_SCOPES = ["model", "tool", "tenant"] as const

type Envelope = { degraded: boolean; reason: string | null }

type Overview = Envelope & {
  scope: string | null
  totalRuns: number | null
  byStatus: Record<string, number>
  auditEntries: number | null
  queueWaitSeconds: number | null
  queueRunId: string | null
}

type TenantRow = { tenantId: string; runs: number | null; lastActivityAt: string | null }
type Tenants = Envelope & { rows: TenantRow[] | null }

type RunRow = {
  id: string
  tenantId: string | null
  status: string
  kind: string | null
  attempt: number | null
  createdAt: string | null
}
type Runs = Envelope & { rows: RunRow[] | null }

type Quotas = Envelope & {
  requestsPerMinute: number | null
  windowSeconds: number | null
  dailyTokenBudget: number | null
  tokensUsed: number | null
  tokensRemaining: number | null
  usageRatio: number | null
  unlimited: boolean | null
  rateWindowReason: string | null
}

type AuditRow = {
  id: number | null
  ts: string | null
  userId: string | null
  action: string | null
  resource: string | null
  runId: string | null
}
type Chain = {
  ok: boolean | null
  brokenAt: number | null
  reason: string | null
  degraded: boolean | null
  reasonCode: string | null
  chainStartAt: string | null
  uncoveredCount: number | null
}
type AuditSummary = Envelope & {
  entries: number | null
  rows: AuditRow[] | null
  chain: Chain | null
}

type Toggle = { scope: string | null; target: string | null; tenantId: string | null; reason: string | null; actor: string | null }
type Toggles = Envelope & { rows: Toggle[] | null }

class HttpError extends Error {
  status: number
  constructor(status: number, url: string) {
    super(`${url} HTTP ${status}`)
    this.status = status
  }
}

async function fetchJson(url: string): Promise<unknown> {
  const r = await fetch(url, { cache: "no-store", headers: authHeaders() })
  if (!r.ok) throw new HttpError(r.status, url)
  return await r.json()
}

function isNum(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v)
}

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function asArray(v: unknown): unknown[] {
  return Array.isArray(v) ? v : []
}

function asString(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v : null
}

function envelope(r: Record<string, unknown>): Envelope {
  const reason = asString(r.reason) ?? (Array.isArray(r.reasons) ? asString(r.reasons[0]) : null)
  return { degraded: r.degraded === true, reason }
}

function num(v: unknown): string {
  return isNum(v) ? String(v) : "--"
}

function pct(v: unknown, digits = 1): string {
  return isNum(v) ? `${(v * 100).toFixed(digits)}%` : "--"
}

function waitText(v: unknown): string {
  return isNum(v) ? `${v.toFixed(1)}s` : "--"
}

function text(v: unknown): string {
  return asString(v) ?? "--"
}

function parseOverview(raw: unknown): Overview | null {
  const r = asRecord(raw)
  if (!r) return null
  const runs = asRecord(r.runs)
  const byStatusRaw = runs ? asRecord(runs.by_status) : null
  const byStatus: Record<string, number> = {}
  for (const status of RUN_STATUSES) {
    const value = byStatusRaw?.[status]
    byStatus[status] = isNum(value) ? value : 0
  }
  const audit = asRecord(r.audit)
  const queue = asRecord(r.queue)
  return {
    ...envelope(r),
    scope: asString(r.scope),
    totalRuns: runs && isNum(runs.total) ? runs.total : null,
    byStatus,
    auditEntries: audit && isNum(audit.entries) ? audit.entries : null,
    queueWaitSeconds: queue && isNum(queue.oldest_queued_wait_seconds) ? queue.oldest_queued_wait_seconds : null,
    queueRunId: queue ? asString(queue.oldest_queued_run_id) : null,
  }
}

function parseTenants(raw: unknown): Tenants | null {
  const r = asRecord(raw)
  if (!r) return null
  const rows = Array.isArray(r.tenants)
    ? asArray(r.tenants).map(item => {
        const row = asRecord(item) ?? {}
        return {
          tenantId: text(row.tenant_id),
          runs: isNum(row.runs) ? row.runs : null,
          lastActivityAt: asString(row.last_activity_at),
        }
      })
    : null
  return { ...envelope(r), rows }
}

function parseRuns(raw: unknown): Runs | null {
  const r = asRecord(raw)
  if (!r) return null
  const rows = Array.isArray(r.runs)
    ? asArray(r.runs).map(item => {
        const row = asRecord(item) ?? {}
        return {
          id: text(row.id),
          tenantId: asString(row.tenant_id),
          status: text(row.status),
          kind: asString(row.kind),
          attempt: isNum(row.attempt) ? row.attempt : null,
          createdAt: asString(row.created_at),
        }
      })
    : null
  return { ...envelope(r), rows }
}

function parseQuotas(raw: unknown): Quotas | null {
  const r = asRecord(raw)
  if (!r) return null
  const policy = asRecord(r.policy)
  return {
    ...envelope(r),
    requestsPerMinute: policy && isNum(policy.requests_per_minute) ? policy.requests_per_minute : null,
    windowSeconds: policy && isNum(policy.window_seconds) ? policy.window_seconds : null,
    dailyTokenBudget: policy && isNum(policy.daily_token_budget) ? policy.daily_token_budget : null,
    tokensUsed: isNum(r.tokens_used) ? r.tokens_used : null,
    tokensRemaining: isNum(r.tokens_remaining) ? r.tokens_remaining : null,
    usageRatio: isNum(r.usage_ratio) ? r.usage_ratio : null,
    unlimited: typeof r.unlimited === "boolean" ? r.unlimited : null,
    rateWindowReason: asString(r.rate_window_reason),
  }
}

function parseAudit(raw: unknown): AuditSummary | null {
  const r = asRecord(raw)
  if (!r) return null
  const rows = Array.isArray(r.recent)
    ? asArray(r.recent).map(item => {
        const row = asRecord(item) ?? {}
        return {
          id: isNum(row.id) ? row.id : null,
          ts: asString(row.ts),
          userId: asString(row.user_id),
          action: asString(row.action),
          resource: asString(row.resource),
          runId: asString(row.run_id),
        }
      })
    : null
  const c = asRecord(r.chain)
  const chain: Chain | null = c
    ? {
        ok: typeof c.ok === "boolean" ? c.ok : null,
        brokenAt: isNum(c.broken_at) ? c.broken_at : null,
        reason: asString(c.reason),
        degraded: typeof c.degraded === "boolean" ? c.degraded : null,
        reasonCode: asString(c.reason_code),
        chainStartAt: asString(c.chain_start_at),
        uncoveredCount: isNum(c.uncovered_count) ? c.uncovered_count : null,
      }
    : null
  return {
    ...envelope(r),
    entries: isNum(r.entries) ? r.entries : null,
    rows,
    chain,
  }
}

function parseToggles(raw: unknown): Toggles | null {
  const r = asRecord(raw)
  if (!r) return null
  const rows = Array.isArray(r.toggles)
    ? asArray(r.toggles).map(item => {
        const row = asRecord(item) ?? {}
        return {
          scope: asString(row.scope),
          target: asString(row.target),
          tenantId: asString(row.tenant_id),
          reason: asString(row.reason),
          actor: asString(row.actor),
        }
      })
    : null
  return { ...envelope(r), rows }
}

function Card({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4 backdrop-blur">
      <div className="text-[11px] tracking-[0.14em] text-slate-400">{label}</div>
      <div className="mt-1 font-display text-xl font-semibold text-mist">{value}</div>
      {sub ? <div className="mt-1 font-mono text-[11px] text-slate-500">{sub}</div> : null}
    </div>
  )
}

export default function Admin() {
  const [overview, setOverview] = useState<Overview | null>(null)
  const [tenants, setTenants] = useState<Tenants | null>(null)
  const [quotas, setQuotas] = useState<Quotas | null>(null)
  const [audit, setAudit] = useState<AuditSummary | null>(null)
  const [toggles, setToggles] = useState<Toggles | null>(null)
  const [runs, setRuns] = useState<Runs | null>(null)
  const [runsStatus, setRunsStatus] = useState("")
  const [runsLimit, setRunsLimit] = useState(50)
  const [loading, setLoading] = useState(true)
  const [errors, setErrors] = useState<string[]>([])
  const [forbidden, setForbidden] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    let aborted = false
    async function load() {
      setLoading(true)
      setErrors([])
      setForbidden(false)
      const targets: [string, (v: unknown) => void][] = [
        [API.overview, v => setOverview(parseOverview(v))],
        [API.tenants, v => setTenants(parseTenants(v))],
        [API.quotas, v => setQuotas(parseQuotas(v))],
        [API.audit, v => setAudit(parseAudit(v))],
        [API.toggles, v => setToggles(parseToggles(v))],
      ]
      const results = await Promise.allSettled(targets.map(([url]) => fetchJson(url)))
      if (aborted) return
      const nextErrors: string[] = []
      results.forEach((result, index) => {
        const [url, apply] = targets[index]
        if (result.status === "fulfilled") {
          apply(result.value)
          return
        }
        const err = result.reason
        if (err instanceof HttpError && err.status === 403) setForbidden(true)
        nextErrors.push(err instanceof Error ? err.message : `${url} 请求失败`)
      })
      setErrors(nextErrors)
      setLoading(false)
    }
    load()
    return () => {
      aborted = true
    }
  }, [reloadKey])

  useEffect(() => {
    let aborted = false
    async function loadRuns() {
      try {
        const data = await fetchJson(API.runs(runsStatus, runsLimit))
        if (!aborted) setRuns(parseRuns(data))
      } catch (err) {
        if (aborted) return
        if (err instanceof HttpError && err.status === 403) setForbidden(true)
        setRuns(null)
        setErrors(prev => [...prev, err instanceof Error ? err.message : "runs 请求失败"])
      }
    }
    loadRuns()
    return () => {
      aborted = true
    }
  }, [runsStatus, runsLimit, reloadKey])

  const degradedReasons = [
    overview?.degraded ? `总览：${overview.reason ?? "unknown"}` : null,
    tenants?.degraded ? `租户：${tenants.reason ?? "unknown"}` : null,
    runs?.degraded ? `队列：${runs.reason ?? "unknown"}` : null,
    quotas?.degraded ? `配额：${quotas.reason ?? "unknown"}` : null,
    audit?.degraded ? `审计：${audit.reason ?? "unknown"}` : null,
    toggles?.degraded ? `开关：${toggles.reason ?? "unknown"}` : null,
  ].filter((item): item is string => typeof item === "string")

  const [toggleScope, setToggleScope] = useState<(typeof TOGGLE_SCOPES)[number]>("model")
  const [toggleTarget, setToggleTarget] = useState("")
  const [toggleReason, setToggleReason] = useState("")
  const [toggleAction, setToggleAction] = useState<"enable" | "disable">("enable")
  const [toggleMessage, setToggleMessage] = useState<string | null>(null)
  const [toggleBusy, setToggleBusy] = useState(false)

  async function submitToggle(event: FormEvent) {
    event.preventDefault()
    setToggleBusy(true)
    setToggleMessage(null)
    try {
      const r = await fetch(API.toggles, {
        method: "POST",
        cache: "no-store",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          scope: toggleScope,
          target: toggleTarget.trim(),
          action: toggleAction,
          reason: toggleReason.trim(),
        }),
      })
      const body = asRecord(await r.json().catch(() => ({}))) ?? {}
      if (!r.ok) {
        const detail = asRecord(body.detail)
        setToggleMessage(`操作失败：${text(detail?.reason ?? body.reason)}（HTTP ${r.status}）`)
        return
      }
      const audited = body.audit_recorded === true
      setToggleMessage(
        audited
          ? `已${toggleAction === "enable" ? "停用" : "恢复"} ${toggleScope}:${toggleTarget}，审计已记录`
          : `已${toggleAction === "enable" ? "停用" : "恢复"} ${toggleScope}:${toggleTarget}，但审计写入失败`,
      )
      setToggleTarget("")
      setToggleReason("")
      setReloadKey(k => k + 1)
    } catch (err) {
      setToggleMessage(`操作失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setToggleBusy(false)
    }
  }

  if (forbidden) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-10">
        <div data-testid="admin-403" className="rounded-2xl border border-red-400/20 bg-red-400/[0.07] p-5">
          <h2 className="text-sm font-semibold text-red-200">管理后台 · 权限不足（403）</h2>
          <p className="mt-2 text-sm leading-6 text-slate-300">
            本页需要 <span className="font-mono text-mist">admin</span> 角色；后端已拒绝请求。
            角色以服务端裁决为准，前端只做提示。
          </p>
        </div>
      </div>
    )
  }

  const chain = audit?.chain
  const auditCoverage =
    chain && chain.chainStartAt
      ? `自 ${chain.chainStartAt} 起算，此前 ${num(chain.uncoveredCount)} 条未覆盖`
      : `链上暂无可校验区间，此前 ${num(chain?.uncoveredCount)} 条未覆盖`
  const chainState = !chain
    ? "--"
    : chain.ok === true
      ? "链校验通过"
      : chain.ok === false
        ? `链校验未通过${chain.reason ? `（${chain.reason}）` : ""}`
        : "--"

  return (
    <div className="mx-auto max-w-7xl px-6 py-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-xl font-semibold text-mist">Admin · 管理后台</h1>
          <p className="mt-1 text-sm text-slate-400">
            总览 · 租户 · runs 看板 · 配额 · 审计链 · killswitch
          </p>
        </div>
        <button
          type="button"
          onClick={() => setReloadKey(k => k + 1)}
          className="rounded-full border border-white/10 bg-white/5 px-3 py-1.5 text-xs text-slate-300 hover:bg-white/10"
        >
          刷新
        </button>
      </div>

      {(degradedReasons.length > 0 || errors.length > 0) && !loading ? (
        <div className="mt-4 space-y-2">
          {degradedReasons.length > 0 ? (
            <DegradedBanner reason={degradedReasons.join(" / ")} source="/v1/admin" />
          ) : null}
          {errors.length > 0 ? (
            <div role="alert" className="rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-2.5 text-sm text-rose-200">
              部分数据获取失败：{errors.join("；")}
            </div>
          ) : null}
        </div>
      ) : null}

      <section className="mt-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Card label="runs 总数" value={loading ? "…" : num(overview?.totalRuns)} sub={overview?.scope ? `范围：${overview.scope}` : "范围 --"} />
        <Card label="排队中" value={loading ? "…" : num(overview?.byStatus.queued)} sub="queued" />
        <Card label="审计条数" value={loading ? "…" : num(overview?.auditEntries)} sub="audit_log" />
        <Card
          label="最老排队等待"
          value={loading ? "…" : waitText(overview?.queueWaitSeconds)}
          sub={
            loading
              ? "…"
              : overview?.queueRunId
                ? `run ${overview.queueRunId}`
                : overview?.totalRuns == null
                  ? "数据不可用"
                  : "无排队任务"
          }
        />
      </section>

      <section className="mt-4 grid grid-cols-3 gap-2 md:grid-cols-7">
        {RUN_STATUSES.map(status => (
          <div key={status} className="rounded-xl border border-white/5 bg-white/[0.03] p-3 text-center">
            <div className="text-[11px] text-slate-400">{status}</div>
            <div className="mt-1 font-semibold text-mist">{loading ? "…" : num(overview?.byStatus[status])}</div>
          </div>
        ))}
      </section>

      <section className="mt-6 rounded-2xl border border-white/10 bg-ink-800/60 p-4 backdrop-blur">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-sm font-semibold text-mist">runs 只读看板</h2>
          <div className="flex items-center gap-2 text-xs">
            <select
              aria-label="run 状态"
              value={runsStatus}
              onChange={e => setRunsStatus(e.target.value)}
              className="rounded-lg border border-white/10 bg-ink-900 px-2 py-1 text-slate-200"
            >
              <option value="">全部状态</option>
              {RUN_STATUSES.map(status => (
                <option key={status} value={status}>
                  {status}
                </option>
              ))}
            </select>
            <select
              aria-label="条数上限"
              value={runsLimit}
              onChange={e => setRunsLimit(Number(e.target.value))}
              className="rounded-lg border border-white/10 bg-ink-900 px-2 py-1 text-slate-200"
            >
              {[20, 50, 100].map(n => (
                <option key={n} value={n}>
                  {n} 条
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-slate-400">
              <tr>
                <th className="py-1.5 pr-3">id</th>
                <th className="py-1.5 pr-3">状态</th>
                <th className="py-1.5 pr-3">kind</th>
                <th className="py-1.5 pr-3">attempt</th>
                <th className="py-1.5 pr-3">租户</th>
                <th className="py-1.5">created_at</th>
              </tr>
            </thead>
            <tbody className="font-mono text-slate-200">
              {(runs?.rows ?? []).map(row => (
                <tr key={row.id} className="border-t border-white/5">
                  <td className="py-1.5 pr-3">{row.id}</td>
                  <td className="py-1.5 pr-3">{row.status}</td>
                  <td className="py-1.5 pr-3">{text(row.kind)}</td>
                  <td className="py-1.5 pr-3">{num(row.attempt)}</td>
                  <td className="py-1.5 pr-3">{text(row.tenantId)}</td>
                  <td className="py-1.5">{text(row.createdAt)}</td>
                </tr>
              ))}
              {runs?.rows && runs.rows.length === 0 ? (
                <tr>
                  <td colSpan={6} className="py-3 text-center text-slate-500">
                    暂无 run
                  </td>
                </tr>
              ) : null}
              {runs?.rows == null ? (
                <tr>
                  <td colSpan={6} className="py-3 text-center text-slate-500">
                    --
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </section>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <section className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
          <h2 className="text-sm font-semibold text-mist">租户</h2>
          <table className="mt-3 w-full text-left text-xs">
            <thead className="text-slate-400">
              <tr>
                <th className="py-1.5 pr-3">tenant_id</th>
                <th className="py-1.5 pr-3">runs</th>
                <th className="py-1.5">最后活动</th>
              </tr>
            </thead>
            <tbody className="font-mono text-slate-200">
              {(tenants?.rows ?? []).map(row => (
                <tr key={row.tenantId} className="border-t border-white/5">
                  <td className="py-1.5 pr-3">{row.tenantId}</td>
                  <td className="py-1.5 pr-3">{num(row.runs)}</td>
                  <td className="py-1.5">{text(row.lastActivityAt)}</td>
                </tr>
              ))}
              {tenants?.rows == null ? (
                <tr>
                  <td colSpan={3} className="py-3 text-center text-slate-500">
                    --
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </section>

        <section className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
          <h2 className="text-sm font-semibold text-mist">配额 · 当前租户</h2>
          <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
            <div className="rounded-xl border border-white/5 bg-white/[0.03] p-3">
              <div className="text-slate-400">速率窗口</div>
              <div className="mt-1 text-mist">
                {quotas?.requestsPerMinute != null && quotas?.windowSeconds != null
                  ? `${quotas.requestsPerMinute} 次 / ${quotas.windowSeconds}s`
                  : "--"}
              </div>
            </div>
            <div className="rounded-xl border border-white/5 bg-white/[0.03] p-3">
              <div className="text-slate-400">窗口内用量</div>
              <div className="mt-1 text-mist" title={quotas?.rateWindowReason ?? undefined}>
                --
              </div>
            </div>
            <div className="rounded-xl border border-white/5 bg-white/[0.03] p-3">
              <div className="text-slate-400">今日 token</div>
              <div className="mt-1 text-mist">
                {num(quotas?.tokensUsed)} / {quotas?.unlimited ? "不限" : num(quotas?.dailyTokenBudget)}
              </div>
            </div>
            <div className="rounded-xl border border-white/5 bg-white/[0.03] p-3">
              <div className="text-slate-400">预算使用率</div>
              <div className="mt-1 text-mist">{quotas?.unlimited ? "不限" : pct(quotas?.usageRatio)}</div>
            </div>
          </div>
        </section>
      </div>

      <section className="mt-4 rounded-2xl border border-amber-400/20 bg-amber-400/10 p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-amber-200">审计摘要</h2>
          <span className="font-mono text-[11px] text-amber-200/80">条数 {num(audit?.entries)}</span>
        </div>
        <p className="mt-2 font-mono text-xs text-amber-100/90">{auditCoverage}</p>
        <p className="mt-1 text-xs text-amber-100/70">
          {chainState}
          {chain?.degraded ? ` · 扫描降级：${chain.reasonCode ?? "unknown"}` : ""}
          {chain?.brokenAt != null ? ` · 断点 ${chain.brokenAt}` : ""}
        </p>
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-amber-200/70">
              <tr>
                <th className="py-1 pr-3">ts</th>
                <th className="py-1 pr-3">action</th>
                <th className="py-1 pr-3">user</th>
                <th className="py-1 pr-3">resource</th>
                <th className="py-1">run</th>
              </tr>
            </thead>
            <tbody className="font-mono text-amber-50/90">
              {(audit?.rows ?? []).map((row, index) => (
                <tr key={`${row.id ?? "row"}-${index}`} className="border-t border-amber-400/10">
                  <td className="py-1 pr-3">{text(row.ts)}</td>
                  <td className="py-1 pr-3">{text(row.action)}</td>
                  <td className="py-1 pr-3">{text(row.userId)}</td>
                  <td className="py-1 pr-3">{text(row.resource)}</td>
                  <td className="py-1">{text(row.runId)}</td>
                </tr>
              ))}
              {audit?.rows == null ? (
                <tr>
                  <td colSpan={5} className="py-2 text-center text-amber-200/60">
                    --
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </section>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <section className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
          <h2 className="text-sm font-semibold text-mist">生效中的开关</h2>
          <ul className="mt-3 space-y-1.5 text-xs">
            {(toggles?.rows ?? []).map((row, index) => (
              <li key={`${row.scope}-${row.target}-${index}`} className="rounded-xl border border-white/5 bg-white/[0.03] px-3 py-2">
                <span className="font-mono text-mist">{text(row.scope)}</span>
                <span className="mx-2 text-slate-500">/</span>
                <span className="font-mono text-mist">{text(row.target)}</span>
                <div className="mt-0.5 text-slate-400">
                  {text(row.reason)} · {text(row.actor)} · {text(row.tenantId)}
                </div>
              </li>
            ))}
            {toggles?.rows && toggles.rows.length === 0 ? (
              <li className="text-slate-500">当前没有生效中的开关</li>
            ) : null}
            {toggles?.rows == null ? <li className="text-slate-500">--</li> : null}
          </ul>
        </section>

        <section className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
          <h2 className="text-sm font-semibold text-mist">启停开关</h2>
          <form onSubmit={submitToggle} className="mt-3 space-y-2 text-xs">
            <div className="grid grid-cols-2 gap-2">
              <label className="flex flex-col gap-1">
                <span className="text-slate-400">scope</span>
                <select
                  aria-label="开关 scope"
                  value={toggleScope}
                  onChange={e => setToggleScope(e.target.value as (typeof TOGGLE_SCOPES)[number])}
                  className="rounded-lg border border-white/10 bg-ink-900 px-2 py-1.5 text-slate-200"
                >
                  {TOGGLE_SCOPES.map(scope => (
                    <option key={scope} value={scope}>
                      {scope}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-slate-400">action</span>
                <select
                  aria-label="开关动作"
                  value={toggleAction}
                  onChange={e => setToggleAction(e.target.value as "enable" | "disable")}
                  className="rounded-lg border border-white/10 bg-ink-900 px-2 py-1.5 text-slate-200"
                >
                  <option value="enable">enable（停用）</option>
                  <option value="disable">disable（恢复）</option>
                </select>
              </label>
            </div>
            <label className="flex flex-col gap-1">
              <span className="text-slate-400">target</span>
              <input
                aria-label="开关 target"
                value={toggleTarget}
                onChange={e => setToggleTarget(e.target.value)}
                required
                className="rounded-lg border border-white/10 bg-ink-900 px-2 py-1.5 font-mono text-slate-200"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-slate-400">reason</span>
              <input
                aria-label="开关 reason"
                value={toggleReason}
                onChange={e => setToggleReason(e.target.value)}
                required
                className="rounded-lg border border-white/10 bg-ink-900 px-2 py-1.5 text-slate-200"
              />
            </label>
            <button
              type="submit"
              disabled={toggleBusy}
              className="rounded-full border border-amber-500/30 bg-amber-500/20 px-3 py-1.5 text-xs font-medium text-amber-100 hover:bg-amber-500/30 disabled:opacity-50"
            >
              {toggleBusy ? "提交中…" : "提交"}
            </button>
            {toggleMessage ? <p className="text-amber-200/90">{toggleMessage}</p> : null}
          </form>
        </section>
      </div>
    </div>
  )
}
