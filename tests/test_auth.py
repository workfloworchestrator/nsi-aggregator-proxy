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


"""Tests for OIDC JWT authentication and group-based authorization."""

import time
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt import PyJWKClientConnectionError, PyJWKClientError

from aggregator_proxy.auth import OIDCProvider, check_groups
from aggregator_proxy.main import app
from aggregator_proxy.reservation_store import ReservationStore

TEST_ISSUER = "https://idp.example.org"
TEST_AUDIENCE = "test-client-id"
TEST_SUBJECT = "user-123"
TEST_JWKS_URI = "https://idp.example.org/.well-known/jwks.json"
TEST_USERINFO_URI = "https://idp.example.org/userinfo"

_BASE_OIDC_PATCHES: dict[str, Any] = {
    "auth_enabled": True,
    "mtls_header": "",
    "oidc_issuer": TEST_ISSUER,
    "oidc_audience": TEST_AUDIENCE,
    "oidc_jwks_uri": TEST_JWKS_URI,
    "oidc_userinfo_uri": TEST_USERINFO_URI,
    "oidc_required_groups": [],
    "oidc_group_claim": "eduperson_entitlement",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_app_state(app_instance: Any) -> None:
    """Inject mock state objects so endpoints don't crash after auth passes."""
    app_instance.state.nsi_client = AsyncMock()
    app_instance.state.callback_client = AsyncMock()
    app_instance.state.reservation_store = ReservationStore()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture
def make_token(rsa_keypair):
    """Factory to create signed JWTs with sane defaults."""
    private_key, _ = rsa_keypair

    def _make(
        *,
        sub: str = TEST_SUBJECT,
        iss: str = TEST_ISSUER,
        aud: str = TEST_AUDIENCE,
        exp: float | None = None,
        extra_claims: dict | None = None,
    ) -> str:
        now = time.time()
        claims = {
            "sub": sub,
            "iss": iss,
            "aud": aud,
            "exp": exp if exp is not None else now + 3600,
            "iat": now,
        }
        if extra_claims:
            claims.update(extra_claims)
        return pyjwt.encode(claims, private_key, algorithm="RS256")

    return _make


@pytest.fixture
def mock_oidc_provider(rsa_keypair):
    """OIDCProvider mock that returns the test public key for signature verification."""
    _, public_key = rsa_keypair
    provider = AsyncMock(spec=OIDCProvider)

    mock_jwk = AsyncMock()
    mock_jwk.key = public_key
    provider.get_signing_key = AsyncMock(return_value=mock_jwk)
    provider.fetch_userinfo = AsyncMock(return_value={})

    return provider


@contextmanager
def _patched_auth_client(mock_oidc_provider: AsyncMock, *, required_groups: list[str] | None = None):
    """Context manager: patch settings, start TestClient, inject mocks."""
    patches = {**_BASE_OIDC_PATCHES}
    if required_groups is not None:
        patches["oidc_required_groups"] = required_groups

    with (
        patch.multiple("aggregator_proxy.auth.settings", **patches),
        patch("aggregator_proxy.routers.reservations._refresh_all_reservations", return_value=[]),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        _mock_app_state(app)
        app.state.oidc_provider = mock_oidc_provider
        app.state.oidc_http_client = AsyncMock()
        yield client, mock_oidc_provider


@pytest.fixture
def auth_api(mock_oidc_provider):
    """TestClient with OIDC enabled, no required groups."""
    with _patched_auth_client(mock_oidc_provider) as result:
        yield result


@pytest.fixture
def auth_api_with_groups(mock_oidc_provider):
    """TestClient with OIDC enabled and required groups."""
    with _patched_auth_client(mock_oidc_provider, required_groups=["urn:example:admins"]) as result:
        yield result


# ---------------------------------------------------------------------------
# Token extraction tests
# ---------------------------------------------------------------------------


class TestTokenExtraction:
    @pytest.mark.parametrize(
        "auth_header",
        [
            pytest.param(None, id="no-header"),
            pytest.param("", id="empty-header"),
            pytest.param("Basic dXNlcjpwYXNz", id="basic-auth"),
            pytest.param("Bearer ", id="bearer-no-token"),
        ],
    )
    def test_no_bearer_token_rejected_when_auth_enabled(self, auth_api, auth_header):
        """Requests without credentials are rejected when auth is enabled."""
        client, _ = auth_api
        headers = {"Authorization": auth_header} if auth_header is not None else {}
        resp = client.get("/reservations", headers=headers)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Authentication required"


# ---------------------------------------------------------------------------
# Signature validation tests
# ---------------------------------------------------------------------------


class TestSignatureValidation:
    @pytest.mark.parametrize(
        "headers_factory",
        [
            pytest.param(lambda t: {"Authorization": f"Bearer {t}"}, id="authorization-header"),
            pytest.param(lambda t: {"X-Auth-Request-Access-Token": t}, id="access-token-header"),
        ],
    )
    def test_valid_token_returns_200(self, auth_api, make_token, headers_factory):
        client, _ = auth_api
        resp = client.get("/reservations", headers=headers_factory(make_token()))
        assert resp.status_code == 200

    def test_tampered_token_returns_401(self, auth_api, mock_oidc_provider):
        mock_oidc_provider.get_signing_key.side_effect = pyjwt.PyJWTError("Invalid header")
        client, _ = auth_api
        resp = client.get("/reservations", headers={"Authorization": "Bearer tampered.jwt.here"})
        assert resp.status_code == 401

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(PyJWKClientError("Failed to fetch JWKS"), id="jwk-client-error"),
            pytest.param(PyJWKClientConnectionError("Connection refused"), id="jwk-connection-error"),
        ],
    )
    def test_jwks_failure_returns_401(self, auth_api, error):
        client, mock_provider = auth_api
        mock_provider.get_signing_key.side_effect = error
        resp = client.get("/reservations", headers={"Authorization": "Bearer some.jwt.token"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Token validation failed"


# ---------------------------------------------------------------------------
# Claim validation tests
# ---------------------------------------------------------------------------


class TestClaimValidation:
    def test_expired_token(self, auth_api, make_token):
        client, _ = auth_api
        token = make_token(exp=time.time() - 3600)
        resp = client.get("/reservations", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Token expired"

    def test_wrong_audience(self, auth_api, make_token):
        client, _ = auth_api
        token = make_token(aud="wrong-audience")
        resp = client.get("/reservations", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid audience"

    def test_wrong_issuer(self, auth_api, make_token):
        client, _ = auth_api
        token = make_token(iss="https://evil.example.org")
        resp = client.get("/reservations", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid issuer"

    def test_missing_sub_claim(self, auth_api, rsa_keypair):
        private_key, _ = rsa_keypair
        token = pyjwt.encode(
            {"iss": TEST_ISSUER, "aud": TEST_AUDIENCE, "exp": time.time() + 3600},
            private_key,
            algorithm="RS256",
        )
        client, _ = auth_api
        resp = client.get("/reservations", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Group authorization tests
# ---------------------------------------------------------------------------


class TestGroupAuthorization:
    def test_user_in_required_group(self, auth_api_with_groups, make_token):
        client, mock_provider = auth_api_with_groups
        mock_provider.fetch_userinfo.return_value = {"eduperson_entitlement": ["urn:example:admins"]}
        headers = {
            "Authorization": f"Bearer {make_token()}",
            "X-Auth-Request-Access-Token": "access-token-value",
        }
        resp = client.get("/reservations", headers=headers)
        assert resp.status_code == 200

    def test_user_not_in_required_group(self, auth_api_with_groups, make_token):
        client, mock_provider = auth_api_with_groups
        mock_provider.fetch_userinfo.return_value = {"eduperson_entitlement": ["urn:example:viewers"]}
        headers = {
            "Authorization": f"Bearer {make_token()}",
            "X-Auth-Request-Access-Token": "access-token-value",
        }
        resp = client.get("/reservations", headers=headers)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Insufficient group membership"

    def test_missing_access_token_header(self, auth_api_with_groups, make_token):
        client, _ = auth_api_with_groups
        resp = client.get("/reservations", headers={"Authorization": f"Bearer {make_token()}"})
        assert resp.status_code == 401
        assert "access token" in resp.json()["detail"].lower()

    def test_group_claim_missing_from_userinfo(self, auth_api_with_groups, make_token):
        client, mock_provider = auth_api_with_groups
        mock_provider.fetch_userinfo.return_value = {"sub": "user-123"}
        headers = {
            "Authorization": f"Bearer {make_token()}",
            "X-Auth-Request-Access-Token": "access-token-value",
        }
        resp = client.get("/reservations", headers=headers)
        assert resp.status_code == 403

    def test_string_group_claim_handled(self, auth_api_with_groups, make_token):
        client, mock_provider = auth_api_with_groups
        mock_provider.fetch_userinfo.return_value = {"eduperson_entitlement": "urn:example:admins"}
        headers = {
            "Authorization": f"Bearer {make_token()}",
            "X-Auth-Request-Access-Token": "access-token-value",
        }
        resp = client.get("/reservations", headers=headers)
        assert resp.status_code == 200

    def test_userinfo_fetch_failure(self, auth_api_with_groups, make_token):
        client, mock_provider = auth_api_with_groups
        mock_provider.fetch_userinfo.side_effect = httpx.HTTPError("connection refused")
        headers = {
            "Authorization": f"Bearer {make_token()}",
            "X-Auth-Request-Access-Token": "access-token-value",
        }
        resp = client.get("/reservations", headers=headers)
        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert detail == "Failed to fetch user information"
        assert "connection refused" not in detail

    def test_no_groups_required_skips_check(self, auth_api, make_token):
        client, mock_provider = auth_api
        resp = client.get("/reservations", headers={"Authorization": f"Bearer {make_token()}"})
        assert resp.status_code == 200
        mock_provider.fetch_userinfo.assert_not_called()

    def test_userinfo_called_per_request_with_mock(self, auth_api_with_groups, make_token):
        """Mock provider's fetch_userinfo is called each request (real caching is in OIDCProvider)."""
        client, mock_provider = auth_api_with_groups
        mock_provider.fetch_userinfo.return_value = {"eduperson_entitlement": ["urn:example:admins"]}
        headers = {
            "Authorization": f"Bearer {make_token()}",
            "X-Auth-Request-Access-Token": "same-access-token",
        }
        client.get("/reservations", headers=headers)
        client.get("/reservations", headers=headers)
        assert mock_provider.fetch_userinfo.call_count == 2


# ---------------------------------------------------------------------------
# mTLS verification tests
# ---------------------------------------------------------------------------


def _mtls_only_client(extra_patches: dict[str, Any] | None = None):
    """Context manager for mTLS-only tests (no OIDC issuer)."""
    patches = {**_BASE_OIDC_PATCHES, "mtls_header": "X-Auth-Method", "oidc_issuer": "", **(extra_patches or {})}

    @contextmanager
    def _ctx():
        with (
            patch.multiple("aggregator_proxy.auth.settings", **patches),
            patch("aggregator_proxy.routers.reservations._refresh_all_reservations", return_value=[]),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            _mock_app_state(app)
            app.state.oidc_provider = None
            app.state.oidc_http_client = None
            yield client

    return _ctx()


def _dual_auth_client(mock_oidc_provider: AsyncMock):
    """Context manager for tests with both OIDC and mTLS configured."""
    patches = {**_BASE_OIDC_PATCHES, "mtls_header": "X-Auth-Method"}

    @contextmanager
    def _ctx():
        with (
            patch.multiple("aggregator_proxy.auth.settings", **patches),
            patch("aggregator_proxy.routers.reservations._refresh_all_reservations", return_value=[]),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            _mock_app_state(app)
            app.state.oidc_provider = mock_oidc_provider
            app.state.oidc_http_client = AsyncMock()
            yield client

    return _ctx()


class TestMTLSVerification:
    @pytest.mark.parametrize(
        ("headers", "expected_status"),
        [
            pytest.param({"X-Auth-Method": "mTLS", "X-Client-DN": "CN=Test"}, 200, id="valid-header"),
            pytest.param({"X-Auth-Method": "mTLS"}, 200, id="valid-header-no-client-dn"),
            pytest.param({}, 401, id="no-header"),
            pytest.param({"X-Auth-Method": ""}, 401, id="empty-value"),
            pytest.param({"X-Auth-Method": "   "}, 401, id="whitespace-value"),
        ],
    )
    def test_mtls_only(self, headers, expected_status):
        """mTLS-only mode (no OIDC issuer): header presence determines access."""
        with _mtls_only_client() as client:
            resp = client.get("/reservations", headers=headers)
            assert resp.status_code == expected_status

    def test_jwt_valid_no_mtls_header_returns_200(self, mock_oidc_provider, make_token):
        """Valid JWT succeeds even without mTLS header configured."""
        with _patched_auth_client(mock_oidc_provider) as (client, _):
            resp = client.get("/reservations", headers={"Authorization": f"Bearer {make_token()}"})
            assert resp.status_code == 200

    def test_mtls_and_oidc_both_configured_jwt_wins(self, mock_oidc_provider, make_token):
        """When both methods are configured, valid JWT takes the OIDC path."""
        with _dual_auth_client(mock_oidc_provider) as client:
            resp = client.get("/reservations", headers={"Authorization": f"Bearer {make_token()}"})
            assert resp.status_code == 200
            mock_oidc_provider.get_signing_key.assert_called_once()

    def test_mtls_fallback_when_no_jwt(self, mock_oidc_provider):
        """When both methods are configured and no JWT present, mTLS header is checked."""
        with _dual_auth_client(mock_oidc_provider) as client:
            resp = client.get("/reservations", headers={"X-Auth-Method": "mTLS", "X-Client-DN": "CN=Test"})
            assert resp.status_code == 200
            mock_oidc_provider.get_signing_key.assert_not_called()

    def test_invalid_jwt_not_rescued_by_mtls(self, mock_oidc_provider, make_token):
        """Invalid JWT is rejected even when mTLS header is present."""
        mock_oidc_provider.get_signing_key.side_effect = pyjwt.PyJWTError("bad signature")
        with _dual_auth_client(mock_oidc_provider) as client:
            headers = {"Authorization": "Bearer tampered.jwt.here", "X-Auth-Method": "mTLS"}
            resp = client.get("/reservations", headers=headers)
            assert resp.status_code == 401

    def test_no_methods_configured_returns_401(self):
        """Auth enabled with neither OIDC nor mTLS configured rejects all requests."""
        with _mtls_only_client(extra_patches={"mtls_header": ""}) as client:
            resp = client.get("/reservations")
            assert resp.status_code == 401
            assert resp.json()["detail"] == "Authentication required"


# ---------------------------------------------------------------------------
# Auth disabled tests
# ---------------------------------------------------------------------------


class TestAuthDisabled:
    def test_no_auth_required_when_disabled(self):
        with (
            patch("aggregator_proxy.routers.reservations._refresh_all_reservations", return_value=[]),
            TestClient(app, raise_server_exceptions=True) as client,
        ):
            _mock_app_state(app)
            resp = client.get("/reservations")
            assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Health endpoint (unprotected)
# ---------------------------------------------------------------------------


class TestHealthWithAuth:
    def test_health_returns_200_without_token(self, auth_api):
        client, _ = auth_api
        resp = client.get("/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# NSI callback endpoint (unprotected)
# ---------------------------------------------------------------------------


class TestCallbackWithAuth:
    def test_callback_accessible_without_token(self, auth_api):
        """The NSI callback endpoint must not require authentication."""
        client, _ = auth_api
        resp = client.post("/nsi/v2/callback", content=b"<not-xml/>")
        assert resp.status_code != 401


# ---------------------------------------------------------------------------
# check_groups unit tests
# ---------------------------------------------------------------------------


class TestCheckGroups:
    @pytest.mark.parametrize(
        ("userinfo", "required", "claim", "expected"),
        [
            pytest.param({"g": ["admin", "user"]}, ["admin"], "g", ["admin"], id="list-match"),
            pytest.param({"g": "admin"}, ["admin"], "g", ["admin"], id="string-match"),
            pytest.param({"g": ["viewer"]}, ["admin"], "g", None, id="no-match"),
            pytest.param({}, ["admin"], "g", None, id="claim-missing"),
            pytest.param({"g": ["a", "b", "c"]}, ["c", "d"], "g", ["c"], id="partial-intersection"),
        ],
    )
    def test_check_groups(self, userinfo, required, claim, expected):
        if expected is not None:
            assert check_groups(userinfo, required, claim) == expected
        else:
            with pytest.raises(Exception, match="403"):
                check_groups(userinfo, required, claim)


# ---------------------------------------------------------------------------
# Config validation tests
# ---------------------------------------------------------------------------


class TestConfigValidation:
    @pytest.mark.parametrize(
        ("input_val", "expected"),
        [
            pytest.param('["g1","g2"]', ["g1", "g2"], id="json-array"),
            pytest.param("g1,g2,g3", ["g1", "g2", "g3"], id="comma-separated"),
            pytest.param("single-group", ["single-group"], id="single-value"),
            pytest.param("", [], id="empty-string"),
        ],
    )
    def test_required_groups_parsing(self, input_val, expected):
        from aggregator_proxy.settings import Settings

        s = Settings(oidc_required_groups=input_val)  # type: ignore[call-arg]
        assert s.oidc_required_groups == expected

    def test_malformed_json_groups_raises_error(self):
        from pydantic import ValidationError

        from aggregator_proxy.settings import Settings

        with pytest.raises(ValidationError, match="Invalid JSON"):
            Settings(oidc_required_groups='["g1", invalid')  # type: ignore[call-arg]
