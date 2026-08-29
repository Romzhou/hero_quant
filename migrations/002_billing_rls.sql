-- 002_billing_rls.sql — billing PG + RLS
CREATE TABLE IF NOT EXISTS factors (
  factor_id text PRIMARY KEY,
  name text NOT NULL,
  price double precision NOT NULL,
  tenant text NOT NULL,
  description text DEFAULT ''
);
CREATE TABLE IF NOT EXISTS purchases (
  id SERIAL PRIMARY KEY,
  factor_id text NOT NULL REFERENCES factors(factor_id),
  buyer_tenant text NOT NULL,
  tenant text NOT NULL,
  price double precision NOT NULL,
  created_at timestamptz DEFAULT now()
);
-- Enable RLS + FORCE (owner 也受限，防 bypass)
ALTER TABLE factors ENABLE ROW LEVEL SECURITY;
ALTER TABLE factors FORCE ROW LEVEL SECURITY;
ALTER TABLE purchases ENABLE ROW LEVEL SECURITY;
ALTER TABLE purchases FORCE ROW LEVEL SECURITY;
-- RLS policy tenant_isolation — 必须 SET LOCAL app.tenant，且非空
-- 应用层在事务开始时执行: SET LOCAL app.tenant = '<tenant>';
-- IS NOT NULL 防止未设置时全表可见；WITH CHECK 防止写入跨租户
DROP POLICY IF EXISTS tenant_isolation ON factors;
CREATE POLICY tenant_isolation ON factors
  USING (tenant = current_setting('app.tenant', true) AND current_setting('app.tenant', true) IS NOT NULL)
  WITH CHECK (tenant = current_setting('app.tenant', true) AND current_setting('app.tenant', true) IS NOT NULL);
DROP POLICY IF EXISTS tenant_isolation ON purchases;
CREATE POLICY tenant_isolation ON purchases
  USING (
    tenant = current_setting('app.tenant', true)
    AND buyer_tenant = current_setting('app.tenant', true)
    AND current_setting('app.tenant', true) IS NOT NULL
  )
  WITH CHECK (
    tenant = current_setting('app.tenant', true)
    AND buyer_tenant = current_setting('app.tenant', true)
    AND current_setting('app.tenant', true) IS NOT NULL
  );
-- 兼容旧版 app.current_tenant 的别名设置（若代码仍用 app.current_tenant）
-- 建议统一使用 app.tenant；此处不另建策略，应用层需 SET LOCAL 两个 key 以兼容
-- 迁移说明：存量记录 tenant 为空的需先回填；否则 FORCE RLS 下将不可见
