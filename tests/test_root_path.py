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

    def test_docs_available_with_root_path(self) -> None:
        app = _make_app("/aggregator-proxy")
        with TestClient(app) as client:
            _install_app_state(app)
            resp = client.get("/docs")
            assert resp.status_code == 200
            assert "swagger" in resp.text.lower()
