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

"""Tests for the authentication dependencies.

Authentication is performed at the edge proxy (Traefik + oauth2-proxy for the
portal route, nsi-auth for the mTLS route) and the resulting identity is
forwarded as request headers. ``get_authenticated_user`` reads
``X-Auth-Request-Email`` plus ``X-Auth-Request-Groups`` on the OIDC path, or
the configured ``mtls_header`` (with ``X-Client-DN``) on the mTLS path.
``get_mtls_authenticated_callback`` is strict-mTLS-only: it must reject
OIDC users even if they manage to reach ``/nsi/v2/callback`` through the
portal HTTPRoute.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from aggregator_proxy.auth import check_groups


def _mock_nsi_client() -> AsyncMock:
    """Mock NSI client so reservations routes don't try to talk to a real aggregator."""
    return AsyncMock()


def _mock_reservation_store() -> AsyncMock:
    """Mock reservation store so the routes' DI is satisfied."""
    return AsyncMock()


@contextmanager
def _auth_client(**setting_overrides: Any) -> Iterator[TestClient]:
    """TestClient with auth.settings patched and stub app state installed."""
    base: dict[str, Any] = {
        "proxy_auth_enabled": True,
        "mtls_header": "",
        "oidc_required_groups": [],
    }
    base.update(setting_overrides)
    from aggregator_proxy.main import app

    with (
        patch.multiple("aggregator_proxy.auth.settings", **base),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        app.state.nsi_client = _mock_nsi_client()
        app.state.callback_client = AsyncMock()
        app.state.reservation_store = _mock_reservation_store()
        yield client


# ---------------------------------------------------------------------------
# get_authenticated_user — OIDC + mTLS branches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "required_groups", "mtls_header", "expected_status"),
    [
        pytest.param(
            {"X-Auth-Request-Email": "alice@example.org"},
            [],
            "",
            200,
            id="oidc-email-no-groups-required",
        ),
        pytest.param(
            {"X-Auth-Request-Email": "alice@example.org", "X-Auth-Request-Groups": "developer,viewer"},
            ["developer"],
            "",
            200,
            id="oidc-group-match-comma-separated",
        ),
        pytest.param(
            {"X-Auth-Request-Email": "alice@example.org", "X-Auth-Request-Groups": "developer viewer"},
            ["viewer"],
            "",
            200,
            id="oidc-group-match-space-separated",
        ),
        pytest.param(
            {"X-Auth-Request-Email": "alice@example.org", "X-Auth-Request-Groups": "viewer"},
            ["developer"],
            "",
            403,
            id="oidc-group-mismatch",
        ),
        pytest.param(
            {"X-Auth-Request-Email": "alice@example.org"},
            ["developer"],
            "",
            403,
            id="oidc-groups-header-missing-when-required",
        ),
        pytest.param(
            {},
            [],
            "",
            401,
            id="no-identity-no-mtls",
        ),
        pytest.param(
            {"X-Auth-Method": "mTLS", "X-Client-DN": "CN=Test"},
            [],
            "X-Auth-Method",
            200,
            id="mtls-happy-path",
        ),
        pytest.param(
            {"X-Auth-Method": "mTLS"},
            [],
            "X-Auth-Method",
            200,
            id="mtls-without-client-dn",
        ),
        pytest.param(
            {"X-Auth-Method": "   "},
            [],
            "X-Auth-Method",
            401,
            id="mtls-header-whitespace",
        ),
        pytest.param(
            {"X-Auth-Method": ""},
            [],
            "X-Auth-Method",
            401,
            id="mtls-header-empty",
        ),
        pytest.param(
            {
                "X-Auth-Request-Email": "alice@example.org",
                "X-Auth-Request-Groups": "developer",
                "X-Auth-Method": "mTLS",
            },
            ["developer"],
            "X-Auth-Method",
            200,
            id="oidc-wins-when-both-present",
        ),
        pytest.param(
            {"X-Auth-Request-Email": "   "},
            [],
            "",
            401,
            id="oidc-whitespace-email-treated-as-absent",
        ),
        pytest.param(
            {"X-Auth-Request-Email": "   "},
            [],
            "X-Auth-Method",
            401,
            id="oidc-whitespace-email-does-not-fall-through-to-mtls",
        ),
        pytest.param(
            {},
            [],
            "X-Auth-Method",
            401,
            id="mtls-configured-but-header-missing",
        ),
        pytest.param(
            {"X-Auth-Request-Email": "alice@example.org", "X-Client-DN": "CN=spoofed"},
            [],
            "X-Auth-Method",
            200,
            id="x-client-dn-cannot-divert-to-mtls-branch",
        ),
        pytest.param(
            {"X-Auth-Request-Email": "alice@example.org", "X-Auth-Request-Groups": "developer , viewer "},
            ["viewer"],
            "",
            200,
            id="oidc-groups-tolerate-whitespace-around-commas",
        ),
    ],
)
def test_get_authenticated_user(
    headers: dict[str, str],
    required_groups: list[str],
    mtls_header: str,
    expected_status: int,
) -> None:
    with _auth_client(oidc_required_groups=required_groups, mtls_header=mtls_header) as client:
        resp = client.get("/openapi.json", headers=headers)
        assert resp.status_code == expected_status


def test_401_response_advertises_bearer_scheme() -> None:
    with _auth_client() as client:
        resp = client.get("/openapi.json")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == "Bearer"


# ---------------------------------------------------------------------------
# get_mtls_authenticated_callback — strict mTLS on /nsi/v2/callback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "mtls_header", "expected_status"),
    [
        pytest.param(
            {"X-Auth-Method": "mTLS", "X-Client-DN": "CN=Aggregator"},
            "X-Auth-Method",
            # 400 = SOAP body is empty; we only care that the dep let us through.
            400,
            id="callback-mtls-header-present-reaches-handler",
        ),
        pytest.param(
            {},
            "X-Auth-Method",
            401,
            id="callback-no-headers-rejected",
        ),
        pytest.param(
            {"X-Auth-Request-Email": "alice@example.org", "X-Auth-Request-Groups": "developer"},
            "X-Auth-Method",
            401,
            id="callback-oidc-must-not-satisfy",
        ),
        pytest.param(
            {"X-Auth-Method": "   "},
            "X-Auth-Method",
            401,
            id="callback-whitespace-mtls-header-rejected",
        ),
        pytest.param(
            {"X-Auth-Method": "mTLS"},
            "",
            401,
            id="callback-mtls-header-not-configured-still-rejects",
        ),
    ],
)
def test_get_mtls_authenticated_callback(
    headers: dict[str, str],
    mtls_header: str,
    expected_status: int,
) -> None:
    with _auth_client(mtls_header=mtls_header) as client:
        resp = client.post("/nsi/v2/callback", headers=headers, content=b"")
        assert resp.status_code == expected_status


def test_auth_disabled_lets_everything_through() -> None:
    from aggregator_proxy.main import app

    with (
        patch.multiple("aggregator_proxy.auth.settings", proxy_auth_enabled=False),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        app.state.nsi_client = _mock_nsi_client()
        app.state.callback_client = AsyncMock()
        app.state.reservation_store = _mock_reservation_store()
        # The docs endpoint is no longer auth-blocked; the auth-disabled path
        # should let it through and serve the schema.
        assert client.get("/openapi.json").status_code == 200
        # Callback dep is also a no-op; empty body yields 400 from the handler.
        assert client.post("/nsi/v2/callback", content=b"").status_code != 401


def test_health_is_always_unprotected() -> None:
    with _auth_client() as client:
        resp = client.get("/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /openapi.json, /docs, /redoc share the auth dependency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
@pytest.mark.parametrize(
    ("proxy_auth_enabled", "headers", "required_groups", "expected_status"),
    [
        pytest.param(False, {}, [], 200, id="auth-disabled-served-to-everyone"),
        pytest.param(True, {}, [], 401, id="auth-enabled-no-identity"),
        pytest.param(
            True,
            {"X-Auth-Request-Email": "alice@example.org"},
            [],
            200,
            id="auth-enabled-authenticated-no-groups-required",
        ),
        pytest.param(
            True,
            {"X-Auth-Request-Email": "alice@example.org", "X-Auth-Request-Groups": "developer"},
            ["developer"],
            200,
            id="auth-enabled-group-match",
        ),
        pytest.param(
            True,
            {"X-Auth-Request-Email": "alice@example.org", "X-Auth-Request-Groups": "viewer"},
            ["developer"],
            403,
            id="auth-enabled-group-mismatch",
        ),
    ],
)
def test_openapi_and_docs_behind_auth(
    path: str,
    proxy_auth_enabled: bool,
    headers: dict[str, str],
    required_groups: list[str],
    expected_status: int,
) -> None:
    with patch.multiple(
        "aggregator_proxy.auth.settings",
        proxy_auth_enabled=proxy_auth_enabled,
        oidc_required_groups=required_groups,
        mtls_header="",
    ):
        from aggregator_proxy.main import create_app

        scoped_app = create_app()
        with TestClient(scoped_app, raise_server_exceptions=False) as client:
            scoped_app.state.nsi_client = _mock_nsi_client()
            scoped_app.state.callback_client = AsyncMock()
            scoped_app.state.reservation_store = _mock_reservation_store()
            assert client.get(path, headers=headers).status_code == expected_status


# ---------------------------------------------------------------------------
# check_groups unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user_groups", "required", "expected"),
    [
        pytest.param(["admin", "user"], ["admin"], ["admin"], id="single-required-match"),
        pytest.param(["a", "b", "c"], ["c", "d"], ["c"], id="partial-intersection"),
        pytest.param(["c", "a", "b"], ["a", "b"], ["a", "b"], id="result-is-sorted"),
        pytest.param(["viewer"], ["admin"], [], id="no-match-returns-empty"),
        pytest.param([], ["admin"], [], id="empty-user-groups-returns-empty"),
        pytest.param(["admin"], [], [], id="no-required-groups-returns-empty"),
    ],
)
def test_check_groups(user_groups: list[str], required: list[str], expected: list[str]) -> None:
    assert check_groups(user_groups, required) == expected
