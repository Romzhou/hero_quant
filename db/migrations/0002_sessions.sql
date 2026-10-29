-- 0002_sessions.sql — 会话 / 消息 / 运行 / 工具调用持久化（PR-131/132/134）
--
-- 设计：
-- - 全部表带 tenant_id，应用层每条 SQL 强制 tenant 过滤，DB 层再用 RLS FORCE 兜底。
-- - 工具调用幂等键 (tenant_id, idempotency_key) 唯一；重复调用只保留首次行，
--   由 session.store.record_tool_call 返回既有行，不产生第二次副作用。
-- - messages.id 单调递增，可直接作为 SSE Last-Event-ID 的续跑游标。

CREATE TABLE IF NOT EXISTS sessions (
  id text PRIMARY KEY,
  tenant_id text NOT NULL CHECK (tenant_id <> ''),
  user_id text NOT NULL CHECK (user_id <> ''),
  title text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sessions_tenant_updated ON sessions (tenant_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant_user ON sessions (tenant_id, user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id text NOT NULL CHECK (tenant_id <> ''),
  session_id text NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  role text NOT NULL CHECK (role IN ('system','user','assistant','tool')),
  content text NOT NULL DEFAULT '',
  trace_id text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (tenant_id, session_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_trace ON messages (tenant_id, trace_id) WHERE trace_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS runs (
  id text PRIMARY KEY,
  tenant_id text NOT NULL CHECK (tenant_id <> ''),
  session_id text NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  status text NOT NULL CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
  reason text,
  cost numeric(12,4) NOT NULL DEFAULT 0 CHECK (cost >= 0),
  started_at timestamptz,
  ended_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_runs_session ON runs (tenant_id, session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs (tenant_id, status);

CREATE TABLE IF NOT EXISTS tool_calls (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id text NOT NULL CHECK (tenant_id <> ''),
  run_id text NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  tool text NOT NULL CHECK (tool <> ''),
  args_hash text NOT NULL CHECK (args_hash <> ''),
  idempotency_key text NOT NULL CHECK (idempotency_key <> ''),
  status text NOT NULL CHECK (status IN ('PENDING','SUCCESS','FAILED')),
  result_ref text,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls (tenant_id, run_id);

ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages FORCE ROW LEVEL SECURITY;
ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs FORCE ROW LEVEL SECURITY;
ALTER TABLE tool_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE tool_calls FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS sessions_tenant_isolation ON sessions;
CREATE POLICY sessions_tenant_isolation ON sessions
  USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant());

DROP POLICY IF EXISTS messages_tenant_isolation ON messages;
CREATE POLICY messages_tenant_isolation ON messages
  USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant());

DROP POLICY IF EXISTS runs_tenant_isolation ON runs;
CREATE POLICY runs_tenant_isolation ON runs
  USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant());

DROP POLICY IF EXISTS tool_calls_tenant_isolation ON tool_calls;
CREATE POLICY tool_calls_tenant_isolation ON tool_calls
  USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant());
