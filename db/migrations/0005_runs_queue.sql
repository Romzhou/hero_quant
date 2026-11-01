-- 0005_runs_queue.sql — 长任务队列（PR-201/202/203，批次14）
--
-- 设计：
-- - 复用 0002 建好的 runs 表作队列，只加队列必需的列（attempt/max_tries/lease_until/
--   heartbeat_at/payload/result/error/kind），不新建第二张 run 表，避免链A（session.store）
--   与队列两套 run 身份。
-- - 状态机：queued -> running -> succeeded（成功终态；对外 SSE 事件名 run.done）/ dead（DLQ，
--   超过 max_tries）；cancelling -> cancelled。'succeeded'/'failed' 沿用 0002 既有枚举值，
--   不改写链A 现有写入方；'cancelling'/'dead' 为本批新增。
-- - session_id 放宽为可空：长任务（回测/研究）不必挂在某个会话下。
-- - run_steps 是断点幂等表：主键 (run_id, step_name)，写入 ON CONFLICT DO NOTHING；
--   恢复时先查已完成 step 直接跳过。
-- - 领取用 SELECT ... FOR UPDATE SKIP LOCKED；结果写入必须带 attempt 条件（fencing），
--   影响 0 行即丢弃，防止僵尸 worker 脏写。
-- - RLS：两张表都 FORCE RLS。worker 跨租户领取依赖事务级 GUC app.queue_worker='on'
--   （单独的一条 permissive policy，不改动 runs_tenant_isolation），必须用 SET LOCAL
--   在领取事务内设置：事务提交即失效，绝不 SET（会话级）——否则连接回收后 GUC 仍在，
--   下一个复用到该连接的请求会拿到跨租户可见性。
-- - 领取成功后同一事务内立刻 SET LOCAL app.tenant_id=<该行租户>（current_tenant() 已支持该别名，
--   见 0007）；执行期与写结果一律回归普通租户 RLS，不再带 worker GUC。
-- - API 侧连接若检测到 current_setting('app.queue_worker') 非空，必须直接抛异常
--   （防同一连接池被 worker/API 混用）；该 GUC 只允许 worker 自己的连接设置。
-- - 生产部署前置：建独立角色 hero_queue_worker 并把这条 policy 收窄为 TO hero_queue_worker
--   （不要给该角色 BYPASSRLS）；未建角色前保持现状（见 docs/contracts/batch-14.md §2.4）。

ALTER TABLE runs ALTER COLUMN session_id DROP NOT NULL;

ALTER TABLE runs ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'agent';
ALTER TABLE runs ADD COLUMN IF NOT EXISTS attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0);
ALTER TABLE runs ADD COLUMN IF NOT EXISTS max_tries integer NOT NULL DEFAULT 3 CHECK (max_tries >= 1);
ALTER TABLE runs ADD COLUMN IF NOT EXISTS lease_until timestamptz;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS result jsonb;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS error text;

ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_status_check;
ALTER TABLE runs ADD CONSTRAINT runs_status_check
  CHECK (status IN ('queued','running','succeeded','failed','cancelled','cancelling','dead'));

CREATE INDEX IF NOT EXISTS idx_runs_claim ON runs (created_at) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_runs_lease ON runs (lease_until) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS run_steps (
  run_id text NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  tenant_id text NOT NULL CHECK (tenant_id <> ''),
  step_name text NOT NULL CHECK (step_name <> ''),
  status text NOT NULL DEFAULT 'succeeded' CHECK (status IN ('succeeded','failed')),
  attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  output jsonb,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, step_name)
);

CREATE INDEX IF NOT EXISTS idx_run_steps_run ON run_steps (run_id, created_at);

ALTER TABLE run_steps ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_steps FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS run_steps_tenant_isolation ON run_steps;
CREATE POLICY run_steps_tenant_isolation ON run_steps
  USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant());

-- worker 跨租户领取：独立 permissive policy（与上面 tenant 策略是 OR 关系）
DROP POLICY IF EXISTS runs_queue_worker ON runs;
CREATE POLICY runs_queue_worker ON runs
  USING (current_setting('app.queue_worker', true) = 'on')
  WITH CHECK (current_setting('app.queue_worker', true) = 'on');

DROP POLICY IF EXISTS run_steps_queue_worker ON run_steps;
CREATE POLICY run_steps_queue_worker ON run_steps
  USING (current_setting('app.queue_worker', true) = 'on')
  WITH CHECK (current_setting('app.queue_worker', true) = 'on');
