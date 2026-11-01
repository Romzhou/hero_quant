"""admin — 管理后台 API（PR-215）。

只暴露 `router`：路由注册（/v1/admin 与 /admin 双挂）由 api/server.py 统一完成。
"""

from hero_quant.admin.api import router

__all__ = ["router"]
