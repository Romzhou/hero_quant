-- 0007_current_tenant_alias.sql — current_tenant() 增加 app.tenant_id 别名（批次14）
--
-- 背景：0001 的 current_tenant() 只认 app.tenant / app.current_tenant。
-- 队列 worker 领取成功后按约定 SET LOCAL app.tenant_id=<租户>（字段名与 runs.tenant_id 对齐），
-- 若不扩展函数，执行期的普通 RLS 读不到该 GUC，会表现为"查询到 0 行"这种假成功。
-- 只做 CREATE OR REPLACE，不改 0001（已应用迁移内容被改动会被 runner 判 drift）。

CREATE OR REPLACE FUNCTION current_tenant() RETURNS text AS $$
  SELECT COALESCE(
    NULLIF(current_setting('app.tenant', true), ''),
    NULLIF(current_setting('app.current_tenant', true), ''),
    NULLIF(current_setting('app.tenant_id', true), '')
  );
$$ LANGUAGE sql STABLE;
