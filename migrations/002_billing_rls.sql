-- 002_billing_rls.sql — billing PG + RLS
-- 当前租户解析：app.tenant 优先，兼容旧 app.current_tenant；空串视为未设置（fail-closed）
CREATE OR REPLACE FUNCTION current_tenant() RETURNS text AS $$
  SELECT COALESCE(
    NULLIF(current_setting('app.tenant', true), ''),
    NULLIF(current_setting('app.current_tenant', true), '')
  );
$$ LANGUAGE sql STABLE;

CREATE TABLE IF NOT EXISTS factors (
  factor_id text PRIMARY KEY,
  name text NOT NULL,
  -- 金额用 numeric(12,2)，避免 double precision 的 0.1+0.2 舍入误差；非负校验
  price numeric(12,2) NOT NULL CHECK (price >= 0),
  tenant text NOT NULL CHECK (tenant <> ''),
  description text DEFAULT '',
  -- 复合唯一，供 purchases 复合 FK 保证 tenant 一致（防跨租户 orphan 行）
  UNIQUE (factor_id, tenant)
);
CREATE TABLE IF NOT EXISTS purchases (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  factor_id text NOT NULL,
  buyer_tenant text NOT NULL CHECK (buyer_tenant <> ''),
  tenant text NOT NULL CHECK (tenant <> ''),
  price numeric(12,2) NOT NULL CHECK (price >= 0),
  created_at timestamptz DEFAULT now(),
  -- 复合 FK：purchases.tenant 必须与 factors.tenant 一致（防 RLS 隐藏的跨租户 orphan）
  FOREIGN KEY (factor_id, tenant) REFERENCES factors(factor_id, tenant) ON DELETE RESTRICT
);
-- RLS 过滤与 FK 查询索引（原无索引，USING(tenant=...) 会随表增长 seq-scan）
CREATE INDEX IF NOT EXISTS idx_factors_tenant ON factors(tenant);
CREATE INDEX IF NOT EXISTS idx_purchases_tenant ON purchases(tenant);
CREATE INDEX IF NOT EXISTS idx_purchases_buyer ON purchases(buyer_tenant);
CREATE INDEX IF NOT EXISTS idx_purchases_factor ON purchases(factor_id);

-- Enable RLS + FORCE（owner 也受限）
ALTER TABLE factors ENABLE ROW LEVEL SECURITY;
ALTER TABLE factors FORCE ROW LEVEL SECURITY;
ALTER TABLE purchases ENABLE ROW LEVEL SECURITY;
ALTER TABLE purchases FORCE ROW LEVEL SECURITY;

-- factors：仅当前租户可见/可写
DROP POLICY IF EXISTS factors_tenant_isolation ON factors;
CREATE POLICY factors_tenant_isolation ON factors
  USING (tenant = current_tenant())
  WITH CHECK (tenant = current_tenant());

-- purchases：拆分买家/卖家可见性
-- 修复 critical：原 `tenant = app.tenant AND buyer_tenant = app.tenant` 只允许 self-purchase，
-- 正常 marketplace 购买（buyer != seller）无法 INSERT 且双方都不可见。
DROP POLICY IF EXISTS tenant_isolation ON purchases;
-- 买家可见：我买的
CREATE POLICY purchases_buyer_select ON purchases FOR SELECT
  USING (buyer_tenant = current_tenant());
-- 卖家可见：我卖的
CREATE POLICY purchases_seller_select ON purchases FOR SELECT
  USING (tenant = current_tenant());
-- 插入：买家身份写入（seller tenant 由应用显式提供，与 factors 复合 FK 校验一致）
CREATE POLICY purchases_insert ON purchases FOR INSERT
  WITH CHECK (buyer_tenant = current_tenant());
-- 更新/删除：仅卖家可操作（保守）
CREATE POLICY purchases_seller_update ON purchases FOR UPDATE
  USING (tenant = current_tenant())
  WITH CHECK (tenant = current_tenant());
CREATE POLICY purchases_seller_delete ON purchases FOR DELETE
  USING (tenant = current_tenant());

-- 迁移说明：
-- 1. FORCE RLS 下存量 tenant 为空的记录不可见，需先回填（同一事务）：
--    UPDATE factors SET tenant = 'default' WHERE tenant = '';
--    UPDATE purchases SET tenant = 'default' WHERE tenant = '';
--    UPDATE purchases SET buyer_tenant = 'default' WHERE buyer_tenant = '';
-- 2. 管理/服务角色如需绕过 RLS，授予 BYPASSRLS 或加 permissive policy：
--    CREATE POLICY admin_all_factors ON factors TO service_role USING (true) WITH CHECK (true);
--    CREATE POLICY admin_all_purchases ON purchases TO service_role USING (true) WITH CHECK (true);
