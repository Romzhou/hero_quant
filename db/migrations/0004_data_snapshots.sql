-- 0004_data_snapshots.sql — 数据快照清单 + PIT 日线（W3 PR-121/122 小样本）
--
-- 设计：
-- - data_snapshots 是「一次取数」的不可变清单：内容 hash 决定 snapshot_id，
--   同租户同内容重复写入幂等；回测/查询只认 snapshot_id，不直接读 live。
-- - pit_bars 保存该快照的日线；主键 (snapshot_id, symbol, trade_date) 防重复。
--   knowable_at 是该 bar 在现实中「可知」的日期：日线默认等于 trade_date（收盘后可知），
--   回填/修订数据可显式传更晚日期，as-of 查询同时按 trade_date 与 knowable_at 过滤。
-- - 本切片用 PG 承载小样本（6–12 个月、少量标的）；Parquet/DuckDB 全量方案
--   需新增运行时依赖，留待依赖审批后替换存储层，接口保持不变。
-- - 两张表都带 tenant_id + RLS FORCE；跨租户快照不可见。

CREATE TABLE IF NOT EXISTS data_snapshots (
  snapshot_id text PRIMARY KEY,
  tenant_id text NOT NULL CHECK (tenant_id <> ''),
  vendor text NOT NULL CHECK (vendor <> ''),
  symbols text[] NOT NULL CHECK (cardinality(symbols) >= 1),
  start_date date NOT NULL,
  end_date date NOT NULL,
  adjust text NOT NULL DEFAULT 'unknown',
  fetched_at timestamptz NOT NULL DEFAULT now(),
  row_count integer NOT NULL CHECK (row_count >= 0),
  sha256 text NOT NULL CHECK (sha256 <> ''),
  storage_uri text NOT NULL CHECK (storage_uri <> ''),
  meta jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (tenant_id, sha256),
  UNIQUE (tenant_id, snapshot_id),
  CHECK (start_date <= end_date)
);

CREATE INDEX IF NOT EXISTS idx_data_snapshots_tenant_fetched
  ON data_snapshots (tenant_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_data_snapshots_tenant_vendor
  ON data_snapshots (tenant_id, vendor, fetched_at DESC);

CREATE TABLE IF NOT EXISTS pit_bars (
  tenant_id text NOT NULL CHECK (tenant_id <> ''),
  snapshot_id text NOT NULL,
  symbol text NOT NULL CHECK (symbol <> ''),
  trade_date date NOT NULL,
  knowable_at date NOT NULL,
  open numeric(18,6) NOT NULL CHECK (open > 0),
  high numeric(18,6) NOT NULL CHECK (high > 0),
  low numeric(18,6) NOT NULL CHECK (low > 0),
  close numeric(18,6) NOT NULL CHECK (close > 0),
  volume numeric(20,4) NOT NULL CHECK (volume >= 0),
  PRIMARY KEY (snapshot_id, symbol, trade_date),
  FOREIGN KEY (tenant_id, snapshot_id)
    REFERENCES data_snapshots (tenant_id, snapshot_id) ON DELETE CASCADE,
  CHECK (knowable_at >= trade_date),
  CHECK (high >= low),
  CHECK (high >= open AND high >= close),
  CHECK (low <= open AND low <= close)
);

CREATE INDEX IF NOT EXISTS idx_pit_bars_asof
  ON pit_bars (tenant_id, symbol, trade_date, knowable_at);
CREATE INDEX IF NOT EXISTS idx_pit_bars_snapshot
  ON pit_bars (tenant_id, snapshot_id, symbol, trade_date);

ALTER TABLE data_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_snapshots FORCE ROW LEVEL SECURITY;
ALTER TABLE pit_bars ENABLE ROW LEVEL SECURITY;
ALTER TABLE pit_bars FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS data_snapshots_tenant_isolation ON data_snapshots;
CREATE POLICY data_snapshots_tenant_isolation ON data_snapshots
  USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant());

DROP POLICY IF EXISTS pit_bars_tenant_isolation ON pit_bars;
CREATE POLICY pit_bars_tenant_isolation ON pit_bars
  USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant());
