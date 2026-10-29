"""W1/PR-101~104：FastAPI 依赖与 ASGI 中间件的 401/403/租户上下文接线。

用注入 verifier + 本地 HS256 token，不连 Keycloak、不出网。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from hero_quant.auth.context import current_tenant_id, require_tenant
from hero_quant.auth.deps import get_current_principal, install_auth_error_handlers, require_role
from hero_quant.auth.jwt import JWTConfig, JWTVerifier
from hero_quant.auth.middleware import AuthContextMiddleware, _is_public
from hero_quant.auth.oidc import AuthMode
from hero_quant.auth.principal import Principal, Role

NOW = 1_000_000.0
SECRET = "unit-test-secret-value-32-bytes-min"
ISSUER = "https://idp.example/realms/hero"
AUDIENCE = "hero-api"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _token(roles=("viewer",), *, tenant: str | None = "tenant-a", sub: str = "user-1") -> str:
    claims = {"sub": sub, "roles": list(roles), "iss": ISSUER, "aud": AUDIENCE, "exp": int(NOW + 300)}
    if tenant is not None:
        claims["tenant_id"] = tenant
    header = {"alg": "HS256", "typ": "JWT", "kid": "k1"}
    header_segment = _b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_segment = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature = hmac.new(SECRET.encode(), signing_input, hashlib.sha256).digest()
    return f"{header_segment}.{payload_segment}.{_b64url(signature)}"


def _verifier() -> JWTVerifier:
    config = JWTConfig(issuer=ISSUER, audience=AUDIENCE, algorithms=("HS256",), leeway_seconds=0)
    return JWTVerifier(lambda kid: SECRET if kid in ("k1", None) else None, config, clock=lambda: NOW)


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.state.auth_verifier = _verifier()
    install_auth_error_handlers(app)

    @app.get("/viewer")
    async def viewer(principal: Principal = Depends(require_role("viewer"))):
        return {"sub": principal.subject, "tenant": principal.tenant_id}

    @app.get("/analyst")
    async def analyst(principal: Principal = Depends(require_role(Role.ANALYST))):
        return {"sub": principal.subject}

    @app.get("/ctx")
    async def ctx(principal: Principal = Depends(get_current_principal)):
        return {
            "state_tenant": principal.tenant_id,
            "context_tenant": current_tenant_id(),
            "required_tenant": require_tenant(),
            "compat_id": principal.id,
            "compat_tenant": principal.tenant,
        }

    return TestClient(app)


def test_missing_token_returns_401(client: TestClient):
    response = client.get("/viewer")
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"
    assert "Bearer" in response.headers["www-authenticate"]


def test_malformed_authorization_header_returns_401(client: TestClient):
    response = client.get("/viewer", headers={"Authorization": f"Token {_token()}"})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


def test_viewer_allowed_on_viewer_endpoint(client: TestClient):
    response = client.get("/viewer", headers=_auth_header(_token(roles=["viewer"])))
    assert response.status_code == 200
    assert response.json() == {"sub": "user-1", "tenant": "tenant-a"}


def test_viewer_forbidden_on_analyst_endpoint(client: TestClient):
    response = client.get("/analyst", headers=_auth_header(_token(roles=["viewer"])))
    assert response.status_code == 403
    assert response.json()["code"] == "insufficient_role"


def test_admin_satisfies_analyst_requirement(client: TestClient):
    response = client.get("/analyst", headers=_auth_header(_token(roles=["admin"])))
    assert response.status_code == 200


def test_token_without_tenant_returns_401(client: TestClient):
    response = client.get("/viewer", headers=_auth_header(_token(tenant=None)))
    assert response.status_code == 401
    assert response.json()["code"] == "missing_tenant"


def test_principal_bound_to_request_state_and_context(client: TestClient):
    response = client.get("/ctx", headers=_auth_header(_token(roles=["analyst"])))
    assert response.status_code == 200
    body = response.json()
    assert body["state_tenant"] == "tenant-a"
    assert body["context_tenant"] == "tenant-a"
    assert body["required_tenant"] == "tenant-a"
    assert body["compat_id"] == "user-1"
    assert body["compat_tenant"] == "tenant-a"


@pytest.fixture()
def middleware_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(AuthContextMiddleware, verifier=_verifier())

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/secure")
    async def secure():
        return {"tenant": current_tenant_id()}

    return TestClient(app)


def test_middleware_public_path_skips_auth(middleware_client: TestClient):
    assert middleware_client.get("/health").status_code == 200


def test_middleware_rejects_missing_token(middleware_client: TestClient):
    response = middleware_client.get("/secure")
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


def test_middleware_binds_tenant_context(middleware_client: TestClient):
    response = middleware_client.get("/secure", headers=_auth_header(_token()))
    assert response.status_code == 200
    assert response.json()["tenant"] == "tenant-a"


def test_middleware_off_mode_passes_through():
    app = FastAPI()
    app.add_middleware(AuthContextMiddleware, verifier=_verifier(), mode=AuthMode.OFF)

    @app.get("/secure")
    async def secure():
        return {"tenant": current_tenant_id()}

    client = TestClient(app)
    response = client.get("/secure")
    assert response.status_code == 200
    assert response.json()["tenant"] is None


def test_public_path_matching_does_not_open_whole_site():
    public = ("/", "/assets", "/live")
    assert _is_public("/", public)
    assert _is_public("/assets/app.js", public)
    assert _is_public("/live", public)
    assert not _is_public("/v1/query", public)
    assert not _is_public("/livewire", public)
