-- 0008_queue_roles.sql — 队列/管理只读的数据库角色与「TO role」策略（批次15）
--
-- 批次14 用的是事务级 GUC（app.queue_worker='on'）做跨租户领取，那是临时方案：
-- 任何能连库的会话都能 set_config 打开它，等于一个可自助提权的开关。
-- 本迁移换成角色制（用户决策：不用 BYPASSRLS）：
--   hero_queue_worker     —— 只对 runs / run_steps 有 SELECT/INSERT/UPDATE，跨租户领取靠 TO 该角色的 policy；
--   hero_admin_readonly   —— 只读，用于管理后台跨租户看板（TO 该角色 SELECT policy）。
-- 两个角色建为 NOLOGIN：密码与 LOGIN 由部署方用 ALTER ROLE ... PASSWORD 设置，仓库不落任何口令。
-- 迁移本身只做「角色存在性 + 策略 + 授权」，不写密码。

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hero_queue_worker') THEN
    CREATE ROLE hero_queue_worker NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hero_admin_readonly') THEN
    CREATE ROLE hero_admin_readonly NOLOGIN;
  END IF;
END $$;

-- 撤掉批次14 的 GUC 旁路策略：正式方案不再依赖任何会话级开关
DROP POLICY IF EXISTS runs_queue_worker ON runs;
DROP POLICY IF EXISTS run_steps_queue_worker ON run_steps;

CREATE POLICY runs_queue_worker ON runs
  TO hero_queue_worker USING (true) WITH CHECK (true);
CREATE POLICY run_steps_queue_worker ON run_steps
  TO hero_queue_worker USING (true) WITH CHECK (true);

CREATE POLICY runs_admin_readonly ON runs
  FOR SELECT TO hero_admin_readonly USING (true);
CREATE POLICY run_steps_admin_readonly ON run_steps
  FOR SELECT TO hero_admin_readonly USING (true);

GRANT SELECT, INSERT, UPDATE ON runs TO hero_queue_worker;
GRANT SELECT, INSERT, UPDATE ON run_steps TO hero_queue_worker;
GRANT SELECT ON runs TO hero_admin_readonly;
GRANT SELECT ON run_steps TO hero_admin_readonly;
