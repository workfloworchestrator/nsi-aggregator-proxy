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

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aggregator_proxy.settings import Settings


def make_app(root_path: str) -> FastAPI:
    """Create a fresh FastAPI app with the given root_path."""
    from aggregator_proxy.main import health

    app = FastAPI(root_path=root_path)
    app.get("/health", status_code=200, include_in_schema=False)(health)
    return app


class TestRootPathConfig:
    def test_default_root_path_is_empty(self) -> None:
        s = Settings.model_construct()
        assert s.root_path == ""

    def test_root_path_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGGREGATOR_PROXY_ROOT_PATH", "/aggregator-proxy")
        s = Settings()  # type: ignore[call-arg]
        assert s.root_path == "/aggregator-proxy"


class TestRootPathOpenApi:
    def test_openapi_available_without_root_path(self) -> None:
        app = make_app("")
        with TestClient(app) as client:
            resp = client.get("/openapi.json")
            assert resp.status_code == 200
            assert resp.json()["openapi"]

    def test_openapi_available_with_root_path(self) -> None:
        app = make_app("/aggregator-proxy")
        with TestClient(app) as client:
            resp = client.get("/openapi.json")
            assert resp.status_code == 200
            assert resp.json()["openapi"]

    def test_openapi_servers_contains_root_path(self) -> None:
        app = make_app("/aggregator-proxy")
        with TestClient(app) as client:
            spec = client.get("/openapi.json").json()
            server_urls = [s["url"] for s in spec.get("servers", [])]
            assert "/aggregator-proxy" in server_urls

    def test_openapi_no_servers_without_root_path(self) -> None:
        app = make_app("")
        with TestClient(app) as client:
            spec = client.get("/openapi.json").json()
            assert "servers" not in spec or spec["servers"] == [{"url": ""}]


class TestRootPathRoutes:
    def test_health_still_works_with_root_path(self) -> None:
        app = make_app("/aggregator-proxy")
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200

    def test_docs_available_with_root_path(self) -> None:
        app = make_app("/aggregator-proxy")
        with TestClient(app) as client:
            resp = client.get("/docs")
            assert resp.status_code == 200
            assert "swagger" in resp.text.lower()
