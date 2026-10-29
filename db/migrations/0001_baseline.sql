-- 0001_baseline.sql — 收编现有内联 DDL（checkpoint / billing / dedup）
--
-- 说明：本文件是「基线」迁移，只做幂等建表/补列/建策略，绝不删改存量数据。
-- 目的：空库可直接 baseline；旧库（已有内联 DDL 建好的表）执行同样成功并被
-- schema_version 记录为 version=1，从此 schema 由 db/migrations 单源驱动。
-- 约束：不得改写已发布迁移的内容（runner 按 sha256 校验，改动会被判为 drift）。

CREATE TABLE IF NOT EXISTS checkpoints (
  tenant text NOT NULL CHECK (tenant <> ''),
  thread text NOT NULL CHECK (thread <> ''),
  seq int NOT NULL CHECK (seq >= 0),
  checkpoint jsonb NOT NULL,
  run_text TEXT,
  expires_at timestamptz,
  PRIMARY KEY (tenant, thread, seq)
);

ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS run_text TEXT;
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_checkpoints_expires_at ON checkpoints (expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS checkpoints_legacy (
  thread_id TEXT PRIMARY KEY,
  checkpoint JSONB,
  config JSONB,
  expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_legacy_expires_at ON checkpoints_legacy (expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS factors (
  factor_id text PRIMARY KEY,
  name text NOT NULL,
  price numeric(12,2) NOT NULL CHECK (price >= 0),
  tenant text NOT NULL CHECK (tenant <> ''),
  description text DEFAULT '',
  UNIQUE (factor_id, tenant)
);

CREATE TABLE IF NOT EXISTS purchases (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  factor_id text NOT NULL,
  buyer_tenant text NOT NULL CHECK (buyer_tenant <> ''),
  tenant text NOT NULL CHECK (tenant <> ''),
  price numeric(12,2) NOT NULL CHECK (price >= 0),
  idempotency_key text,
  created_at timestamptz DEFAULT now(),
  UNIQUE (factor_id, buyer_tenant),
  FOREIGN KEY (factor_id, tenant) REFERENCES factors(factor_id, tenant) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_factors_tenant ON factors(tenant);
CREATE INDEX IF NOT EXISTS idx_purchases_tenant ON purchases(tenant);
CREATE INDEX IF NOT EXISTS idx_purchases_buyer ON purchases(buyer_tenant);
CREATE INDEX IF NOT EXISTS idx_purchases_factor ON purchases(factor_id);

-- 租户判据函数：billing / dedup / sessions / audit 共用，缺失时 RLS 会拒绝全部行。
CREATE OR REPLACE FUNCTION current_tenant() RETURNS text AS $$
  SELECT COALESCE(
    NULLIF(current_setting('app.tenant', true), ''),
    NULLIF(current_setting('app.current_tenant', true), '')
  );
$$ LANGUAGE sql STABLE;

ALTER TABLE factors ENABLE ROW LEVEL SECURITY;
ALTER TABLE factors FORCE ROW LEVEL SECURITY;
ALTER TABLE purchases ENABLE ROW LEVEL SECURITY;
ALTER TABLE purchases FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS factors_tenant_isolation ON factors;
CREATE POLICY factors_tenant_isolation ON factors
  USING (tenant = current_tenant()) WITH CHECK (tenant = current_tenant());

DROP POLICY IF EXISTS purchases_buyer_select ON purchases;
CREATE POLICY purchases_buyer_select ON purchases FOR SELECT USING (buyer_tenant = current_tenant());
DROP POLICY IF EXISTS purchases_seller_select ON purchases;
CREATE POLICY purchases_seller_select ON purchases FOR SELECT USING (tenant = current_tenant());
DROP POLICY IF EXISTS purchases_insert ON purchases;
CREATE POLICY purchases_insert ON purchases FOR INSERT WITH CHECK (buyer_tenant = current_tenant());
DROP POLICY IF EXISTS purchases_seller_update ON purchases;
CREATE POLICY purchases_seller_update ON purchases FOR UPDATE
  USING (tenant = current_tenant()) WITH CHECK (tenant = current_tenant());
DROP POLICY IF EXISTS purchases_seller_delete ON purchases;
CREATE POLICY purchases_seller_delete ON purchases FOR DELETE USING (tenant = current_tenant());

CREATE TABLE IF NOT EXISTS dedup (
  key TEXT PRIMARY KEY,
  tenant TEXT,
  tool TEXT,
  status TEXT,
  result JSONB,
  updated_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_dedup_status ON dedup (status);
CREATE INDEX IF NOT EXISTS idx_dedup_updated_at ON dedup (updated_at);
CREATE INDEX IF NOT EXISTS idx_dedup_tenant ON dedup (tenant);

CREATE TABLE IF NOT EXISTS tool_call_dedup (
  idempotency_key TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK(status IN ('PENDING','SUCCESS','FAILED')),
  tool TEXT,
  result TEXT,
  error TEXT,
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_tool_status ON tool_call_dedup(status);

ALTER TABLE dedup ENABLE ROW LEVEL SECURITY;
ALTER TABLE tool_call_dedup ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON dedup;
CREATE POLICY tenant_isolation ON dedup
  USING (tenant = current_tenant()) WITH CHECK (tenant = current_tenant());
DROP POLICY IF EXISTS tenant_isolation_insert ON dedup;
CREATE POLICY tenant_isolation_insert ON dedup FOR INSERT
  WITH CHECK (tenant = current_tenant());

DROP POLICY IF EXISTS tenant_isolation ON tool_call_dedup;
CREATE POLICY tenant_isolation ON tool_call_dedup
  USING (split_part(idempotency_key, ':', 1) = current_tenant())
  WITH CHECK (split_part(idempotency_key, ':', 1) = current_tenant());
DROP POLICY IF EXISTS tenant_isolation_insert ON tool_call_dedup;
CREATE POLICY tenant_isolation_insert ON tool_call_dedup FOR INSERT
  WITH CHECK (split_part(idempotency_key, ':', 1) = current_tenant());
