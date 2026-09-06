-- 001_checkpoint.sql — checkpoint PG persistence
-- Task7 DDL, PG default with TTL 7d
-- 信任边界：checkpoint 为进程内部状态，多租户隔离依赖应用层 tenant 主键；
-- 如需 DB 层纵深防御可启用 RLS（见文末示例）。legacy 表已冻结，新代码统一走 checkpoints 表。
CREATE TABLE IF NOT EXISTS checkpoints (
  tenant text NOT NULL CHECK (tenant <> ''),
  thread text NOT NULL CHECK (thread <> ''),
  seq int NOT NULL CHECK (seq >= 0),
  checkpoint jsonb NOT NULL,
  -- TTL 默认 7 天：DB 作为安全网，防止应用漏写 expires_at 产生永生行（原 nullable 无默认）
  expires_at timestamptz NOT NULL DEFAULT now() + INTERVAL '7 days',
  PRIMARY KEY (tenant, thread, seq)
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_expires_at ON checkpoints (expires_at);
-- 清理机制（外部 reaper，未在本 migration 内建 cron）：pg_cron / 定时任务执行
--   DELETE FROM checkpoints WHERE expires_at < now();
-- 或采用 TTL 分区表。

-- Backward compatibility: thread_id 主键表（legacy，已冻结）
-- 注意：legacy 与主表键/可空性不同，存在双写分叉风险；新代码统一走 checkpoints 表，勿再双写。
CREATE TABLE IF NOT EXISTS checkpoints_legacy (
  thread_id TEXT PRIMARY KEY,
  checkpoint JSONB,
  config JSONB,
  expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '7 days'
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_legacy_expires_at ON checkpoints_legacy (expires_at);

-- 可选 RLS 加固示例（若启用 DB 层租户隔离）：
-- ALTER TABLE checkpoints ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY checkpoint_tenant_isolation ON checkpoints
--   USING (tenant = COALESCE(NULLIF(current_setting('app.tenant', true), ''), ''));
