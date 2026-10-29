-- 0003_audit_log.sql — 只追加审计日志（PR-171/173）
--
-- 设计：
-- - 只追加：触发器在 DB 层拒绝 UPDATE / DELETE，应用层不提供改写接口。
-- - 查询维度：tenant_id + run_id 覆盖「按 run_id 查全链路」，tenant_id + ts 覆盖时间检索，
--   tenant_id + user_id 覆盖用户行为检索。
-- - detail 仅存非敏感结构化字段；禁止写入 prompt/结果原文，只存 hash 引用。
-- - 保留策略（≥1 年 / 归档）由 PR-241 的运维任务实现；归档需显式 DISABLE TRIGGER，
--   不允许在应用代码里绕过。

CREATE TABLE IF NOT EXISTS audit_log (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  tenant_id text NOT NULL CHECK (tenant_id <> ''),
  user_id text,
  action text NOT NULL CHECK (action <> ''),
  resource text,
  run_id text,
  model text,
  prompt_hash text,
  result_hash text,
  ip text,
  detail jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_audit_tenant_ts ON audit_log (tenant_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_log (tenant_id, run_id, id);
CREATE INDEX IF NOT EXISTS idx_audit_user_ts ON audit_log (tenant_id, user_id, ts DESC) WHERE user_id IS NOT NULL;

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS audit_log_tenant_isolation ON audit_log;
CREATE POLICY audit_log_tenant_isolation ON audit_log
  USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant());

CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only (attempted %)', TG_OP USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_append_only_trigger ON audit_log;
CREATE TRIGGER audit_log_append_only_trigger
  BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();
