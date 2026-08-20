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

"""Tests for root_path configuration.

Verifies that the FastAPI app respects the ROOT_PATH setting so that
Swagger UI can find the OpenAPI spec when served behind a reverse proxy
with a path prefix (e.g. /aggregator-proxy).
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from aggregator_proxy.settings import Settings


def _make_app(root_path: str) -> FastAPI:
    """Create a fresh aggregator-proxy app with the given root_path and auth disabled."""
    from aggregator_proxy.main import create_app

    with patch.multiple("aggregator_proxy.main.settings", root_path=root_path, proxy_auth_enabled=False):
        return create_app()


def _install_app_state(app: FastAPI) -> None:
    """Install enough state on app.state for the routes to construct (no real I/O)."""
    app.state.nsi_client = AsyncMock()
    app.state.callback_client = AsyncMock()
    app.state.reservation_store = AsyncMock()


class TestRootPathConfig:
    def test_default_root_path_is_empty(self) -> None:
        s = Settings.model_construct()
        assert s.root_path == ""

    def test_root_path_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ROOT_PATH", "/aggregator-proxy")
        s = Settings()  # type: ignore[call-arg]
        assert s.root_path == "/aggregator-proxy"


class TestRootPathOpenApi:
    def test_openapi_available_without_root_path(self) -> None:
        app = _make_app("")
        with TestClient(app) as client:
            _install_app_state(app)
            resp = client.get("/openapi.json")
            assert resp.status_code == 200
            assert resp.json()["openapi"]

    def test_openapi_available_with_root_path(self) -> None:
        app = _make_app("/aggregator-proxy")
        with TestClient(app) as client:
            _install_app_state(app)
            resp = client.get("/openapi.json")
            assert resp.status_code == 200
            assert resp.json()["openapi"]

    def test_openapi_servers_contains_root_path(self) -> None:
        app = _make_app("/aggregator-proxy")
        with TestClient(app) as client:
            _install_app_state(app)
            spec = client.get("/openapi.json").json()
            server_urls = [s["url"] for s in spec.get("servers", [])]
            assert "/aggregator-proxy" in server_urls

    def test_openapi_no_servers_without_root_path(self) -> None:
        app = _make_app("")
        with TestClient(app) as client:
            _install_app_state(app)
            spec = client.get("/openapi.json").json()
            assert "servers" not in spec or spec["servers"] == [{"url": ""}]


class TestRootPathRoutes:
    def test_health_still_works_with_root_path(self) -> None:
        app = _make_app("/aggregator-proxy")
        with TestClient(app) as client:
            _install_app_state(app)
            assert client.get("/health").status_code == 200

    @pytest.mark.parametrize(
        ("root_path", "expected_openapi_url"),
        [
            pytest.param("/aggregator-proxy", "/aggregator-proxy/openapi.json", id="with-prefix"),
            pytest.param("", "/openapi.json", id="no-prefix"),
        ],
    )
    def test_docs_point_at_the_configured_prefix(self, root_path: str, expected_openapi_url: str) -> None:
        app = _make_app(root_path)
        with TestClient(app) as client:
            _install_app_state(app)
            resp = client.get("/docs")
            assert resp.status_code == 200
            assert "swagger" in resp.text.lower()
            assert f"url: '{expected_openapi_url}'" in resp.text


class TestRootPathDoesNotShadowMounts:
    """ROOT_PATH must not reach the request scope, or it shifts mounted sub-apps.

    Setting it on the FastAPI app once broke the MCP mount in production: requests into the
    sub-app 404d while ordinary routes kept answering, so the pod stayed ready. Same rule as
    nsi-mgmt-info and nsi-aura, which hit this with StaticFiles.
    """

    @pytest.mark.parametrize(
        "root_path",
        [pytest.param("", id="no-prefix"), pytest.param("/aggregator-proxy", id="with-prefix")],
    )
    def test_mount_is_reachable_whatever_the_prefix(self, root_path: str) -> None:
        app = _make_app(root_path)
        app.mount("/mcp", Starlette(routes=[Route("/", lambda _: PlainTextResponse("reached"))]))
        with TestClient(app) as client:
            _install_app_state(app)
            resp = client.get("/mcp", follow_redirects=True)
            assert resp.status_code == 200
            assert resp.text == "reached"
            assert client.get("/health").status_code == 200

    def test_openapi_servers_follow_the_configured_prefix(self) -> None:
        app = _make_app("/aggregator-proxy")
        with TestClient(app) as client:
            _install_app_state(app)
            assert client.get("/openapi.json").json()["servers"] == [{"url": "/aggregator-proxy"}]

    def test_openapi_schema_cache_is_not_mutated(self) -> None:
        """Servers is added to a copy; the cached schema must stay clean for other callers."""
        app = _make_app("/aggregator-proxy")
        with TestClient(app) as client:
            _install_app_state(app)
            client.get("/openapi.json")
            assert "servers" not in app.openapi()
