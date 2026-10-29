"""auth.errors — 认证/授权错误类型（fail-closed，不继承 HTTPException）。

职责：定义身份链路的稳定错误码，供 deps/middleware 映射 401/403/429/503。
架构位置：auth 包最底层，被 principal/jwt/jwks/oidc/deps/quota 复用。
关键设计：错误码与 HTTP 状态解耦（同一错误在中间件与依赖里映射一致）；
异常消息只含静态原因，禁止携带 token 原文或密钥材料。
"""

from __future__ import annotations


class AuthError(Exception):
    """认证/授权错误基类；code 为稳定机器码，API 层据此映射状态码。"""

    code = "auth_error"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message or self.code)
        if code:
            self.code = code


class InvalidToken(AuthError):
    """Bearer token 缺失/格式错误/签名或声明校验失败（一律 401）。"""

    code = "invalid_token"


class MissingTenant(AuthError):
    """token 未携带 tenant 且无显式租户上下文（fail-closed，绝不猜默认租户）。"""

    code = "missing_tenant"


class InsufficientRole(AuthError):
    """已认证但角色不足（403）。"""

    code = "insufficient_role"


class QuotaExceeded(AuthError):
    """租户配额耗尽（429），retry_after 用于 Retry-After 头。"""

    code = "quota_exceeded"

    def __init__(self, message: str = "", *, retry_after: int = 60, code: str | None = None) -> None:
        super().__init__(message or self.code, code=code)
        try:
            retry = int(retry_after)
        except (TypeError, ValueError):
            retry = 60
        self.retry_after = max(0, retry)


class AuthConfigError(AuthError):
    """服务端身份配置缺失/不可用（503，不引导客户端重试登录）。"""

    code = "auth_config_error"
