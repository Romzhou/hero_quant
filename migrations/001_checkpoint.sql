-- 001_checkpoint.sql — checkpoint PG persistence
-- Task7 minimal DDL, PG default with TTL 7d
CREATE TABLE IF NOT EXISTS checkpoints (
  tenant text NOT NULL,
  thread text NOT NULL,
  seq int NOT NULL,
  checkpoint jsonb NOT NULL,
  expires_at timestamptz,
  PRIMARY KEY (tenant, thread, seq)
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_expires_at ON checkpoints (expires_at);
-- Backward compatibility: thread_id primary key table for legacy code paths
CREATE TABLE IF NOT EXISTS checkpoints_legacy (
  thread_id TEXT PRIMARY KEY,
  checkpoint JSONB,
  config JSONB,
  expires_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_legacy_expires_at ON checkpoints_legacy (expires_at);
