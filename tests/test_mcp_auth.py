# Copyright 2026 SURF
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""Tests for MCP authentication wiring."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from aggregator_proxy.main import app


async def test_event_hook_registered_when_auth_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """When auth_enabled=true, build_mcp must wire the token-forwarding event hook."""
    from aggregator_proxy import mcp_server
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "mcp_auth_enabled", False)

    captured: dict = {}

    real_from_fastapi = mcp_server.FastMCP.from_fastapi

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["httpx_client_kwargs"] = kwargs.get("httpx_client_kwargs", {})
        return real_from_fastapi(*args, **kwargs)

    monkeypatch.setattr(mcp_server.FastMCP, "from_fastapi", spy)

    mcp_server.build_mcp(app)

    hooks = captured["httpx_client_kwargs"].get("event_hooks", {})
    assert "request" in hooks, "expected request event hook to be registered"
    assert len(hooks["request"]) == 1


async def test_no_event_hook_when_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """When auth_enabled=false, no event hook is needed."""
    from aggregator_proxy import mcp_server
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "mcp_auth_enabled", False)

    captured: dict = {}

    real_from_fastapi = mcp_server.FastMCP.from_fastapi

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["httpx_client_kwargs"] = kwargs.get("httpx_client_kwargs", {})
        return real_from_fastapi(*args, **kwargs)

    monkeypatch.setattr(mcp_server.FastMCP, "from_fastapi", spy)

    mcp_server.build_mcp(app)

    hooks = captured["httpx_client_kwargs"].get("event_hooks", {})
    assert "request" not in hooks or not hooks["request"]


async def test_forward_user_token_no_mcp_context_leaves_request_unchanged() -> None:
    """When no MCP request context is in flight, the hook is a no-op."""
    from aggregator_proxy.mcp_server import _forward_user_token

    outgoing = httpx.Request("GET", "http://example.com")
    original_headers = dict(outgoing.headers)

    await _forward_user_token(outgoing)

    assert dict(outgoing.headers) == original_headers
    assert "Authorization" not in outgoing.headers


async def test_forward_user_token_missing_incoming_request_leaves_request_unchanged() -> None:
    """When the MCP context exists but has no .request attribute, the hook is a no-op."""
    from fastmcp.server.context import request_ctx

    from aggregator_proxy.mcp_server import _forward_user_token

    fake_ctx = SimpleNamespace(request=None)
    token_obj = request_ctx.set(fake_ctx)
    try:
        outgoing = httpx.Request("GET", "http://example.com")
        original_headers = dict(outgoing.headers)

        await _forward_user_token(outgoing)

        assert dict(outgoing.headers) == original_headers
        assert "Authorization" not in outgoing.headers
    finally:
        request_ctx.reset(token_obj)


async def test_forward_user_token_missing_authorization_header_leaves_request_unchanged() -> None:
    """When the incoming MCP request has no Authorization header, the hook is a no-op."""
    from fastmcp.server.context import request_ctx

    from aggregator_proxy.mcp_server import _forward_user_token

    incoming = SimpleNamespace(headers={})
    fake_ctx = SimpleNamespace(request=incoming)
    token_obj = request_ctx.set(fake_ctx)
    try:
        outgoing = httpx.Request("GET", "http://example.com")
        original_headers = dict(outgoing.headers)

        await _forward_user_token(outgoing)

        assert dict(outgoing.headers) == original_headers
        assert "Authorization" not in outgoing.headers
    finally:
        request_ctx.reset(token_obj)


async def test_forward_user_token_copies_authorization_header() -> None:
    """When the incoming MCP request has an Authorization header, it is copied onto the outgoing request."""
    from fastmcp.server.context import request_ctx

    from aggregator_proxy.mcp_server import _forward_user_token

    incoming = SimpleNamespace(headers={"authorization": "Bearer test-token"})
    fake_ctx = SimpleNamespace(request=incoming)
    token_obj = request_ctx.set(fake_ctx)
    try:
        outgoing = httpx.Request("GET", "http://example.com")

        await _forward_user_token(outgoing)

        assert outgoing.headers["Authorization"] == "Bearer test-token"
    finally:
        request_ctx.reset(token_obj)


async def test_mcp_has_auth_provider_when_mcp_auth_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """When mcp_auth_enabled=true, the FastMCP server is configured with an auth provider."""
    from aggregator_proxy import mcp_server
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "mcp_auth_enabled", True)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", "https://idp.example.com")
    monkeypatch.setattr(settings, "oidc_audience", "test-audience")
    monkeypatch.setattr(settings, "oidc_jwks_uri", "https://idp.example.com/jwks")

    mcp = mcp_server.build_mcp(app)

    assert mcp.auth is not None, "expected MCP server to have an auth provider configured"
    assert mcp.auth.jwks_uri == "https://idp.example.com/jwks"
    assert mcp.auth.issuer == "https://idp.example.com"
    assert mcp.auth.audience == "test-audience"


async def test_mcp_has_no_auth_provider_when_mcp_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """When mcp_auth_enabled=false, the FastMCP server has no auth provider."""
    from aggregator_proxy import mcp_server
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "mcp_auth_enabled", False)
    monkeypatch.setattr(settings, "auth_enabled", False)

    mcp = mcp_server.build_mcp(app)

    assert mcp.auth is None


def test_startup_rejects_auth_enabled_without_mcp_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """If REST auth is on but MCP auth is off, startup must refuse.

    We call ``_validate_mcp_settings`` directly rather than reloading the
    ``aggregator_proxy.main`` module: ``importlib.reload`` replaces the global
    ``settings`` instance and ``app`` object, but the routers (imported once at
    module load) keep references to the original ``settings`` instance, which
    causes hard-to-debug test pollution across the suite.
    """
    from aggregator_proxy.main import _validate_mcp_settings
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "mcp_auth_enabled", False)

    with pytest.raises(SystemExit, match="AGGREGATOR_PROXY_MCP_AUTH_ENABLED"):
        _validate_mcp_settings()


def test_startup_rejects_mcp_auth_without_explicit_jwks_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP auth requires explicit OIDC_JWKS_URI (no lifespan auto-discovery available at module load)."""
    from aggregator_proxy.main import _validate_mcp_settings
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "mcp_auth_enabled", True)
    monkeypatch.setattr(settings, "oidc_jwks_uri", "")

    with pytest.raises(SystemExit, match="AGGREGATOR_PROXY_OIDC_JWKS_URI"):
        _validate_mcp_settings()


def test_startup_rejects_mcp_auth_with_required_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP auth with non-empty OIDC_REQUIRED_GROUPS is rejected because group checks cannot succeed.

    JWTVerifier doesn't call userinfo, and the token-forwarding hook only forwards
    ``Authorization`` — not ``X-Auth-Request-Access-Token``. ``get_authenticated_user``
    would always 401 on the internal call, so fail at startup with a clear message.
    """
    from aggregator_proxy.main import _validate_mcp_settings
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "mcp_auth_enabled", True)
    monkeypatch.setattr(settings, "oidc_jwks_uri", "https://idp.example.com/jwks")
    monkeypatch.setattr(settings, "oidc_required_groups", ["urn:example:group"])

    with pytest.raises(SystemExit, match="AGGREGATOR_PROXY_OIDC_REQUIRED_GROUPS"):
        _validate_mcp_settings()


def test_validate_mcp_settings_accepts_valid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """When auth_enabled, mcp_auth_enabled, and oidc_jwks_uri are all set, validation passes."""
    from aggregator_proxy.main import _validate_mcp_settings
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "mcp_auth_enabled", True)
    monkeypatch.setattr(settings, "oidc_jwks_uri", "https://idp.example.com/jwks")
    monkeypatch.setattr(settings, "oidc_required_groups", [])

    _validate_mcp_settings()


def test_mcp_path_returns_404_when_disabled() -> None:
    """When mcp_enabled=false (the default in tests), the /mcp path is not mounted."""
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.get("/mcp")
    assert response.status_code == 404
