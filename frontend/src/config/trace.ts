/**
 * 追踪事件真实性判定（E线）
 * - 职责：区分后端 /v1/trace/events 推送的「真实 trace 文件事件」与「合成心跳」。
 * - 背景：后端 server.py:2770-2790 在找不到 trace.jsonl 时会推送 3 条合成心跳（msg="heartbeat · ..."、ts=""），
 *   真实 trace 记录则带 ts 与 tool/msg。若不判定，监控页会把合成心跳当成真实事件并摘掉「演示数据」标记（假实时）。
 * - 判定顺序：显式 synthetic:true → 演示；显式 non_synthetic:true → 真实；
 *   心跳文案 → 演示；其余要求 ts 为非空字符串（trace.jsonl 记录自带时间戳）才算真实，缺失一律保守判为演示。
 */
export function isRealTraceEvent(payload: unknown): boolean {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false
  const o = payload as Record<string, unknown>
  if (o.synthetic === true) return false
  if (o.non_synthetic === true) return true
  if (typeof o.msg === "string" && o.msg.includes("heartbeat")) return false
  const ts = o.ts
  return typeof ts === "string" && ts.trim() !== ""
}
