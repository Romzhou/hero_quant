-- 001_schema.sql — heroquant 库初始化 DDL（checkpoint + billing/RLS）
--
-- 来源（只读复制，migrations/ 原件未改动）：
--   migrations/001_checkpoint.sql  (checkpoints / checkpoints_legacy)
--   migrations/002_billing_rls.sql (factors / purchases / RLS)
-- 差异说明（必须保留，否则运行时报错）：
--   migrations/*.sql 与运行时内联 DDL 早已 drift（见 migrations/README.md）。本文件
--   以运行时 DDL 为准补齐了 3 处，否则代码路径直接失败：
--     1) checkpoints.run_text / checkpoints.version  —— checkpoint/postgres.py:75 DDL_CHECKPOINTS
--     2) purchases.idempotency_key + UNIQUE(factor_id, buyer_tenant)
--        —— billing/service.py:92 DDL_PURCHASES；缺 UNIQUE 时
--        `ON CONFLICT (factor_id, buyer_tenant)` 报 42P10
--     3) purchases 各 policy 的 DROP IF EXISTS（002 只 drop 了 tenant_isolation，
--        其余 policy 重跑会 42710 duplicate_object）—— billing/service.py:108
--
-- 幂等：全部 IF NOT EXISTS / DROP ... IF EXISTS，可重复执行。
-- 入口：docker-compose.yml 的 postgres 服务（profile=local-stack）把本目录只读挂到
--       /docker-entrypoint-initdb.d/；外部 PG 手动执行：
--       psql -U <user> -d heroquant -f scripts/init/001_schema.sql

-- ===========================================================================
-- 1. checkpoint（migrations/001_checkpoint.sql）
-- ===========================================================================
CREATE TABLE IF NOT EXISTS checkpoints (
  tenant text NOT NULL CHECK (tenant <> ''),
  thread text NOT NULL CHECK (thread <> ''),
  seq int NOT NULL CHECK (seq >= 0),
  checkpoint jsonb NOT NULL,
  -- 运行时 DDL 补充列：LLM 侧运行文本快照
  run_text TEXT,
  -- TTL 默认 7 天：DB 作为安全网，防止应用漏写 expires_at 产生永生行
  expires_at timestamptz NOT NULL DEFAULT now() + INTERVAL '7 days',
  PRIMARY KEY (tenant, thread, seq)
);
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS run_text TEXT;
-- T2-3 乐观锁：version 单调递增，UPSERT 用 WHERE version<=EXCLUDED.version 防并发丢进度
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 0;

-- partial index：仅索引非空 expires_at，提升清理扫描选择性（expires_at NULL 表示永不过期）
CREATE INDEX IF NOT EXISTS idx_checkpoints_expires_at ON checkpoints (expires_at) WHERE expires_at IS NOT NULL;

-- 清理机制（外部 reaper，未在本文件内建 cron）：pg_cron / 定时任务执行
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
CREATE INDEX IF NOT EXISTS idx_checkpoints_legacy_expires_at ON checkpoints_legacy (expires_at) WHERE expires_at IS NOT NULL;

-- 可选 RLS 加固示例（current_tenant() 由下方 billing 段创建；checkpoint 目前依赖应用层
-- tenant 主键隔离，如需 DB 层纵深防御再启用）：
-- ALTER TABLE checkpoints ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY checkpoint_tenant_isolation ON checkpoints
--   USING (tenant = COALESCE(NULLIF(current_setting('app.tenant', true), ''), ''));

-- ===========================================================================
-- 2. billing + RLS（migrations/002_billing_rls.sql）
-- ===========================================================================
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
  -- PR2-F 幂等键：与 UNIQUE(factor_id, buyer_tenant) 一起支撑 ON CONFLICT DO NOTHING
  idempotency_key text,
  created_at timestamptz DEFAULT now(),
  UNIQUE (factor_id, buyer_tenant),
  -- 复合 FK：purchases.tenant 必须与 factors.tenant 一致（防 RLS 隐藏的跨租户 orphan）
  FOREIGN KEY (factor_id, tenant) REFERENCES factors(factor_id, tenant) ON DELETE RESTRICT
);
-- 存量库补列（002 版无此列，运行时 INSERT 直接 42703）
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS idempotency_key text;
-- ON CONFLICT (factor_id, buyer_tenant) 依赖该唯一约束（缺则 42P10）
CREATE UNIQUE INDEX IF NOT EXISTS uq_purchases_factor_buyer ON purchases (factor_id, buyer_tenant);

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
DROP POLICY IF EXISTS purchases_buyer_select ON purchases;
CREATE POLICY purchases_buyer_select ON purchases FOR SELECT
  USING (buyer_tenant = current_tenant());
-- 卖家可见：我卖的
DROP POLICY IF EXISTS purchases_seller_select ON purchases;
CREATE POLICY purchases_seller_select ON purchases FOR SELECT
  USING (tenant = current_tenant());
-- 插入：买家身份写入（seller tenant 由应用显式提供，与 factors 复合 FK 校验一致）
DROP POLICY IF EXISTS purchases_insert ON purchases;
CREATE POLICY purchases_insert ON purchases FOR INSERT
  WITH CHECK (buyer_tenant = current_tenant());
-- 更新/删除：仅卖家可操作（保守）
DROP POLICY IF EXISTS purchases_seller_update ON purchases;
CREATE POLICY purchases_seller_update ON purchases FOR UPDATE
  USING (tenant = current_tenant())
  WITH CHECK (tenant = current_tenant());
DROP POLICY IF EXISTS purchases_seller_delete ON purchases;
CREATE POLICY purchases_seller_delete ON purchases FOR DELETE
  USING (tenant = current_tenant());

-- 迁移说明：
-- 1. FORCE RLS 下存量 tenant 为空的记录不可见，需先回填：
--    UPDATE factors SET tenant = 'default' WHERE tenant = '';
--    UPDATE purchases SET tenant = 'default' WHERE tenant = '';
--    UPDATE purchases SET buyer_tenant = 'default' WHERE buyer_tenant = '';
-- 2. 管理/服务角色如需绕过 RLS，授予 BYPASSRLS 或加 permissive policy：
--    CREATE POLICY admin_all_factors ON factors TO service_role USING (true) WITH CHECK (true);
--    CREATE POLICY admin_all_purchases ON purchases TO service_role USING (true) WITH CHECK (true);
