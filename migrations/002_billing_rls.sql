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
-- Enable RLS
ALTER TABLE factors ENABLE ROW LEVEL SECURITY;
ALTER TABLE purchases ENABLE ROW LEVEL SECURITY;
-- RLS policy tenant_isolation
DROP POLICY IF EXISTS tenant_isolation ON factors;
CREATE POLICY tenant_isolation ON factors USING (tenant = current_setting('app.tenant', true));
DROP POLICY IF EXISTS tenant_isolation ON purchases;
CREATE POLICY tenant_isolation ON purchases USING (tenant = current_setting('app.tenant', true));
-- Force RLS for table owners as well (optional hardening)
-- ALTER TABLE factors FORCE ROW LEVEL SECURITY;
-- ALTER TABLE purchases FORCE ROW LEVEL SECURITY;
