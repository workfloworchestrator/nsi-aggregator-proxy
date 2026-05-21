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


"""Tests for MCP authentication wiring and the claim-translation hook."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from aggregator_proxy.main import app

# ---------------------------------------------------------------------------
# Event-hook wiring
# ---------------------------------------------------------------------------


async def test_event_hook_registered_when_proxy_auth_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """When proxy_auth_enabled=true, build_mcp wires the claim-translation event hook."""
    from aggregator_proxy import mcp_server
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "proxy_auth_enabled", True)
    monkeypatch.setattr(settings, "mcp_auth_enabled", True)
    monkeypatch.setattr(settings, "mcp_oidc_jwks_uri", "https://idp.example.com/jwks")
    monkeypatch.setattr(settings, "mcp_oidc_issuer", "https://idp.example.com")
    monkeypatch.setattr(settings, "mcp_oidc_audience", "test-audience")

    captured: dict = {}
    real_from_fastapi = mcp_server.FastMCP.from_fastapi

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["httpx_client_kwargs"] = kwargs.get("httpx_client_kwargs", {})
        return real_from_fastapi(*args, **kwargs)

    monkeypatch.setattr(mcp_server.FastMCP, "from_fastapi", spy)
    mcp_server.build_mcp(app)

    hooks = captured["httpx_client_kwargs"].get("event_hooks", {})
    assert "request" in hooks
    assert len(hooks["request"]) == 1


async def test_no_event_hook_when_proxy_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With REST auth off, no claim translation is needed and no event hook is registered."""
    from aggregator_proxy import mcp_server
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "proxy_auth_enabled", False)
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


# ---------------------------------------------------------------------------
# AuthProvider construction
# ---------------------------------------------------------------------------


async def test_mcp_has_auth_provider_when_mcp_auth_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from aggregator_proxy import mcp_server
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "proxy_auth_enabled", True)
    monkeypatch.setattr(settings, "mcp_auth_enabled", True)
    monkeypatch.setattr(settings, "mcp_oidc_jwks_uri", "https://idp.example.com/jwks")
    monkeypatch.setattr(settings, "mcp_oidc_issuer", "https://idp.example.com")
    monkeypatch.setattr(settings, "mcp_oidc_audience", "test-audience")

    mcp = mcp_server.build_mcp(app)

    assert mcp.auth is not None
    assert mcp.auth.jwks_uri == "https://idp.example.com/jwks"
    assert mcp.auth.issuer == "https://idp.example.com"
    assert mcp.auth.audience == "test-audience"


async def test_mcp_has_no_auth_provider_when_mcp_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from aggregator_proxy import mcp_server
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "proxy_auth_enabled", False)
    monkeypatch.setattr(settings, "mcp_auth_enabled", False)

    mcp = mcp_server.build_mcp(app)
    assert mcp.auth is None


# ---------------------------------------------------------------------------
# Claim translation hook — _forward_user_identity
# ---------------------------------------------------------------------------


class _StubAccessToken:
    """Minimal stand-in for fastmcp.AccessToken — only ``.claims`` matters here."""

    def __init__(self, claims: dict[str, Any]) -> None:
        self.claims = claims


@pytest.fixture
def _claim_translation_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "mcp_auth_enabled", True)
    monkeypatch.setattr(settings, "mcp_oidc_email_claim", "email")
    monkeypatch.setattr(settings, "mcp_oidc_groups_claim", "groups")


@pytest.fixture
def _claim_translation_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "mcp_auth_enabled", False)


def _patch_access_token(monkeypatch: pytest.MonkeyPatch, claims: dict[str, Any] | None) -> None:
    """Stub ``mcp_server.get_access_token`` to return a token with the given claims (or None)."""
    from aggregator_proxy import mcp_server

    token = _StubAccessToken(claims) if claims is not None else None
    monkeypatch.setattr(mcp_server, "get_access_token", lambda: token)


async def _invoke_hook() -> httpx.Request:
    """Run the hook with a synthetic outgoing request carrying a placeholder Authorization."""
    from aggregator_proxy.mcp_server import _forward_user_identity

    outgoing = httpx.Request("GET", "http://example.com")
    outgoing.headers["Authorization"] = "Bearer placeholder"
    await _forward_user_identity(outgoing)
    return outgoing


@pytest.mark.parametrize(
    ("claims", "expected_email", "expected_groups"),
    [
        pytest.param(
            {"email": "alice@example.org", "groups": ["urn:example:developers", "urn:example:viewers"]},
            "alice@example.org",
            "urn:example:developers,urn:example:viewers",
            id="email-and-list-groups",
        ),
        pytest.param(
            {"email": "alice@example.org"},
            "alice@example.org",
            None,
            id="email-only-no-groups-header",
        ),
        pytest.param(
            {"email": "alice@example.org", "groups": "urn:example:developers"},
            "alice@example.org",
            "urn:example:developers",
            id="groups-as-string-passed-through",
        ),
        pytest.param(
            {"email": "alice@example.org", "groups": {"unexpected": "shape"}},
            "alice@example.org",
            None,
            id="groups-as-unexpected-shape-skipped",
        ),
        pytest.param(
            {"groups": ["urn:example:developers"]},
            None,
            "urn:example:developers",
            id="no-email-still-forwards-groups",
        ),
    ],
)
async def test_forward_user_identity_translates_claims(
    _claim_translation_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
    claims: dict[str, Any],
    expected_email: str | None,
    expected_groups: str | None,
) -> None:
    _patch_access_token(monkeypatch, claims)
    outgoing = await _invoke_hook()

    if expected_email is None:
        assert "X-Auth-Request-Email" not in outgoing.headers
    else:
        assert outgoing.headers["X-Auth-Request-Email"] == expected_email
    if expected_groups is None:
        assert "X-Auth-Request-Groups" not in outgoing.headers
    else:
        assert outgoing.headers["X-Auth-Request-Groups"] == expected_groups
    # Authorization is always dropped on the internal call.
    assert "Authorization" not in outgoing.headers


async def test_forward_user_identity_honors_custom_claim_names(monkeypatch: pytest.MonkeyPatch) -> None:
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "mcp_auth_enabled", True)
    monkeypatch.setattr(settings, "mcp_oidc_email_claim", "preferred_username")
    monkeypatch.setattr(settings, "mcp_oidc_groups_claim", "entitlements")

    _patch_access_token(monkeypatch, {"preferred_username": "alice", "entitlements": ["g1", "g2"]})
    outgoing = await _invoke_hook()

    assert outgoing.headers["X-Auth-Request-Email"] == "alice"
    assert outgoing.headers["X-Auth-Request-Groups"] == "g1,g2"


async def test_forward_user_identity_no_token_is_noop(
    _claim_translation_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When fastmcp has no validated token in context, the hook is a no-op."""
    _patch_access_token(monkeypatch, None)
    outgoing = await _invoke_hook()
    assert "X-Auth-Request-Email" not in outgoing.headers
    assert "X-Auth-Request-Groups" not in outgoing.headers


async def test_forward_user_identity_noop_when_mcp_auth_disabled(
    _claim_translation_disabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth: the hook must not act when MCP auth is off.

    With mcp_auth_enabled=False, fastmcp didn't validate the JWT, so the hook
    must not translate any claims into trusted headers, even though the
    Settings model validator would already have prevented this configuration.
    """
    _patch_access_token(monkeypatch, {"email": "attacker@evil.example", "groups": ["urn:example:developers"]})
    outgoing = await _invoke_hook()

    assert "X-Auth-Request-Email" not in outgoing.headers
    assert "X-Auth-Request-Groups" not in outgoing.headers
    # The hook leaves Authorization alone in this case — it didn't act.
    assert outgoing.headers.get("Authorization") == "Bearer placeholder"


# ---------------------------------------------------------------------------
# /mcp not mounted when disabled
# ---------------------------------------------------------------------------


def test_mcp_path_returns_404_when_disabled() -> None:
    """When mcp_enabled=false (the default in tests), the /mcp path is not mounted."""
    with TestClient(app) as client:
        response = client.get("/mcp")
    assert response.status_code == 404
