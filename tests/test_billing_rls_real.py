"""R8-1【3】billing RLS 实证：非 superuser 角色下验证 factors / purchases 的租户隔离。

为什么需要本文件：llm-pg 的超级用户走 BYPASSRLS，RLS 策略对它根本不生效 —— 现有
`tests/test_billing_persistence.py` 用超级用户连接，"隔离"是应用层 WHERE 的功劳，
证明不了库级隔离。决策 D-2 授权在 llm-pg 建 NOSUPERUSER 角色 `heroquant_app`
（不改现有数据、不动超级用户），本文件用它真连验证：

  1. `set_config('app.tenant', 't1', true)` → 只见 t1 的行；
  2. 切到 t2 → 看不到 t1 的行；
  3. 未设 tenant（`current_tenant()` 为 NULL）→ 0 行，fail-closed。

门控：`HERO_RLS_TEST_DSN` 未配置则整模块 skip 并给出原因，默认 CI 不依赖 PG，不会变红。
"""
import os
import uuid

import pytest

_RLS_DSN = (os.environ.get("HERO_RLS_TEST_DSN") or "").strip()

pytestmark = pytest.mark.skipif(
    not _RLS_DSN,
    reason=(
        "RLS 实证需以非 superuser 角色真连 PG：未配置 HERO_RLS_TEST_DSN"
        "（形如 postgresql://heroquant_app:<pw>@100.114.54.26:5432/heroquant），故 skip"
    ),
)


def _connect():
    """以 RLS 角色建连；autocommit 关闭 —— set_config(..., true) 是事务级，须与后续查询同事务。"""
    import psycopg

    return psycopg.connect(_RLS_DSN, connect_timeout=3)


def _set_tenant(conn, tenant: str | None) -> None:
    """事务级设置租户上下文；None/空串 → current_tenant() 为 NULL（fail-closed 判据）。"""
    conn.execute("SELECT set_config('app.tenant', %s, true)", (tenant or "",))


def test_rls_role_is_not_superuser():
    """前提校验：连接角色必须非 superuser，否则 RLS 被 BYPASSRLS 绕过，后续用例无意义。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT current_user, "
            "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user), "
            "(SELECT rowsecurity FROM pg_tables WHERE tablename = 'factors')"
        ).fetchone()
    user, rolsuper, rowsecurity = row
    assert rolsuper is False, f"RLS 实证必须以非 superuser 连接，当前 {user} 的 rolsuper={rolsuper}"
    assert rowsecurity is True, "factors 表未启用 RLS，本文件断言不成立"


def test_factors_tenant_isolation_under_rls():
    """factors：t1 只见 t1 行；切 t2 看不到 t1；未设 tenant 时 0 行。"""
    suffix = uuid.uuid4().hex[:8]
    t1, t2 = f"rls_a_{suffix}", f"rls_b_{suffix}"
    f1, f2 = f"fa_{suffix}", f"fb_{suffix}"
    with _connect() as conn:
        try:
            # 写入：RLS WITH CHECK 要求 tenant = current_tenant()
            for tenant, fid in ((t1, f1), (t2, f2)):
                _set_tenant(conn, tenant)
                conn.execute(
                    "INSERT INTO factors (factor_id, name, price, tenant, description) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (factor_id) DO NOTHING",
                    (fid, "rls_probe", "1.00", tenant, ""),
                )
            conn.commit()

            # 1) t1 视角：只见自己的行
            _set_tenant(conn, t1)
            ids_t1 = [r[0] for r in conn.execute("SELECT factor_id FROM factors").fetchall()]
            assert f1 in ids_t1, f"t1 未看到自己发布的行：{ids_t1}"
            assert f2 not in ids_t1, f"RLS 失效：t1 看到了 t2 的行 {f2}"

            # 2) t2 视角：看不到 t1 的行
            _set_tenant(conn, t2)
            ids_t2 = [r[0] for r in conn.execute("SELECT factor_id FROM factors").fetchall()]
            assert f2 in ids_t2, f"t2 未看到自己发布的行：{ids_t2}"
            assert f1 not in ids_t2, f"RLS 失效：t2 看到了 t1 的行 {f1}"

            # 3) 未设 tenant：可见行数为 0（fail-closed，不可退化成全表可见）
            _set_tenant(conn, None)
            visible = conn.execute("SELECT count(*) FROM factors").fetchone()[0]
            assert visible == 0, f"未设 tenant 时应 0 行（current_tenant() 为 NULL），实际 {visible}"
        finally:
            for tenant, fid in ((t1, f1), (t2, f2)):
                _set_tenant(conn, tenant)
                conn.execute("DELETE FROM factors WHERE factor_id = %s AND tenant = %s", (fid, tenant))
            conn.commit()


def test_purchases_visible_to_buyer_and_seller_only():
    """purchases：卖方（tenant）与买方（buyer_tenant）可见，无关租户 0 行。"""
    suffix = uuid.uuid4().hex[:8]
    seller, buyer, other = f"sl_{suffix}", f"by_{suffix}", f"ot_{suffix}"
    fid = f"fp_{suffix}"
    with _connect() as conn:
        try:
            _set_tenant(conn, seller)
            conn.execute(
                "INSERT INTO factors (factor_id, name, price, tenant, description) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (factor_id) DO NOTHING",
                (fid, "rls_purchase", "2.00", seller, ""),
            )
            # 买方视角下单：INSERT 策略 WITH CHECK (buyer_tenant = current_tenant())
            _set_tenant(conn, buyer)
            conn.execute(
                "INSERT INTO purchases (factor_id, buyer_tenant, tenant, price, idempotency_key) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (factor_id, buyer_tenant) DO NOTHING",
                (fid, buyer, seller, "2.00", suffix),
            )
            conn.commit()

            # 卖方可见（purchases_seller_select）
            _set_tenant(conn, seller)
            seller_rows = conn.execute(
                "SELECT factor_id FROM purchases WHERE factor_id = %s", (fid,)
            ).fetchall()
            assert len(seller_rows) == 1, f"卖方应看到自己的售出记录，实际 {len(seller_rows)}"

            # 买方可见（purchases_buyer_select）
            _set_tenant(conn, buyer)
            buyer_rows = conn.execute(
                "SELECT factor_id FROM purchases WHERE factor_id = %s", (fid,)
            ).fetchall()
            assert len(buyer_rows) == 1, f"买方应看到自己的购买记录，实际 {len(buyer_rows)}"

            # 无关租户 0 行
            _set_tenant(conn, other)
            other_rows = conn.execute(
                "SELECT factor_id FROM purchases WHERE factor_id = %s", (fid,)
            ).fetchall()
            assert other_rows == [], f"RLS 失效：无关租户看到了交易记录 {other_rows}"

            # 未设 tenant：0 行（fail-closed）
            _set_tenant(conn, None)
            anon_rows = conn.execute("SELECT count(*) FROM purchases").fetchone()[0]
            assert anon_rows == 0, f"未设 tenant 时应 0 行，实际 {anon_rows}"
        finally:
            _set_tenant(conn, seller)
            conn.execute("DELETE FROM purchases WHERE factor_id = %s AND tenant = %s", (fid, seller))
            conn.execute("DELETE FROM factors WHERE factor_id = %s AND tenant = %s", (fid, seller))
            conn.commit()
