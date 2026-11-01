-- 0006_audit_chain.sql — 审计链式哈希（PR-241，批次14）
--
-- 设计：
-- - audit_log 已有 append-only 触发器（0003），任何 UPDATE 都会被 DB 拒绝，所以链哈希
--   必须在 INSERT 前算完：prev_hash 取该租户链头，entry_hash = sha256(prev_hash + 规范化行内容)，
--   两者随行一次写入，不做事后回填/改写。
-- - 规范化内容不含自增 id（INSERT 前未知），按 ts/tenant_id/action/resource/run_id/model/
--   prompt_hash/result_hash/ip/detail 的固定顺序拼接；校验时按 id 升序重算。
-- - 迁移前的历史行 prev_hash / entry_hash 为 NULL，明确表示「链外历史」；校验从首个非 NULL
--   行开始，不伪造历史哈希。
-- - audit_chain_head 保存每租户链头（head_id/head_hash/entries），追加时 SELECT ... FOR UPDATE
--   串行化，避免并发追加分叉。它是派生状态，可删除后由 audit_log 重建。
-- - 保留策略不删除审计行（append-only）；归档需显式 DISABLE TRIGGER，且不得在应用代码里绕过。

ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS prev_hash text;
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS entry_hash text;

CREATE INDEX IF NOT EXISTS idx_audit_chain ON audit_log (tenant_id, id) WHERE entry_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS audit_chain_head (
  tenant_id text PRIMARY KEY,
  head_id bigint NOT NULL CHECK (head_id > 0),
  head_hash text NOT NULL CHECK (head_hash <> ''),
  entries bigint NOT NULL DEFAULT 1 CHECK (entries >= 1),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE audit_chain_head ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_chain_head FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS audit_chain_head_tenant_isolation ON audit_chain_head;
CREATE POLICY audit_chain_head_tenant_isolation ON audit_chain_head
  USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant());
