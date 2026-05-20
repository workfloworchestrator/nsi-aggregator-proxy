# MCP Reservations Endpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose `GET /reservations` and `GET /reservations/{connectionId}` as MCP Resources via a `FastMCP.from_fastapi()` sub-app mounted at `/mcp`, while leaving all other routes hidden from MCP clients.

**Architecture:** A new `aggregator_proxy/mcp_server.py` builds a `FastMCP` instance from the existing FastAPI app, using route maps to keep only the two GET reservation paths (everything else is `MCPType.EXCLUDE`). When `mcp_auth_enabled=true`, a `JWTVerifier`/`OAuthProvider` validates the MCP client's OIDC token, and an httpx event hook forwards that token to the internal `/reservations` call so the existing `get_authenticated_user` dependency re-validates. Lifespans are combined so FastMCP's session-store starts/stops cleanly.

**Tech Stack:** Python 3.13, FastAPI, FastMCP 3.x, uv, pytest (`pytest-asyncio` in auto mode), `pytest-httpx`, structlog, pydantic-settings.

**Spec:** `docs/superpowers/specs/2026-05-20-mcp-reservations-endpoint-design.md`

---

## Task 1: Add fastmcp dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add fastmcp to `[project]` dependencies**

Edit `pyproject.toml`, inserting one line in the `dependencies` list (keep the existing pinned-style format):

```toml
dependencies = [
    "fastapi[standard]==0.136.1",
    "fastmcp==3.2.4",
    "httpx==0.28.1",
    "lxml==6.1.0",
    "pydantic-settings==2.14.1",
    "PyJWT[crypto]==2.12.1",
    "structlog==25.5.0",
    "uvicorn[standard]==0.46.0",
]
```

(If a newer 3.x is available at implementation time, pick the latest stable 3.x release.)

- [ ] **Step 2: Sync and confirm import**

Run: `uv sync && uv run python -c "from fastmcp import FastMCP; from fastmcp.server.providers.openapi import RouteMap, MCPType; from fastmcp.utilities.lifespan import combine_lifespans; print('ok')"`

Expected: `ok` printed, no `ImportError`. If any import fails, the chosen FastMCP version may have moved symbols — check the FastMCP changelog and adjust import paths in later tasks accordingly.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "Add fastmcp dependency for MCP endpoint support"
```

---

## Task 2: Add MCP settings

**Files:**
- Modify: `aggregator_proxy/settings.py`
- Test: `tests/test_settings.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings.py` (or create the file if it doesn't already have a similar pattern — verify first by reading the existing file):

```python
def test_mcp_settings_defaults() -> None:
    from aggregator_proxy.settings import Settings

    s = Settings()
    assert s.mcp_enabled is False
    assert s.mcp_path == "/mcp"
    assert s.mcp_auth_enabled is False


def test_mcp_settings_from_env(monkeypatch) -> None:
    from aggregator_proxy.settings import Settings

    monkeypatch.setenv("AGGREGATOR_PROXY_MCP_ENABLED", "true")
    monkeypatch.setenv("AGGREGATOR_PROXY_MCP_PATH", "/custom-mcp")
    monkeypatch.setenv("AGGREGATOR_PROXY_MCP_AUTH_ENABLED", "true")

    s = Settings()
    assert s.mcp_enabled is True
    assert s.mcp_path == "/custom-mcp"
    assert s.mcp_auth_enabled is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings.py::test_mcp_settings_defaults tests/test_settings.py::test_mcp_settings_from_env -v`

Expected: FAIL with `AttributeError: ... has no attribute 'mcp_enabled'`.

- [ ] **Step 3: Add settings fields**

In `aggregator_proxy/settings.py`, add these three fields to the `Settings` class (place them next to the other optional settings; follow the existing style — type annotations, defaults, no docstring needed):

```python
mcp_enabled: bool = False
mcp_path: str = "/mcp"
mcp_auth_enabled: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings.py -v`

Expected: all settings tests PASS (both the new ones and any existing ones).

- [ ] **Step 5: Commit**

```bash
git add aggregator_proxy/settings.py tests/test_settings.py
git commit -m "Add MCP_ENABLED, MCP_PATH, MCP_AUTH_ENABLED settings"
```

---

## Task 3: Add operation_id to reservation GET routes

**Files:**
- Modify: `aggregator_proxy/routers/reservations.py` (the two `@router.get` decorators on `get_reservation` and `list_reservations`)
- Test: `tests/test_get_reservations.py` (add one assertion at the end)

The `operation_id` becomes the MCP resource/template name. Without an explicit value, FastAPI auto-generates `get_reservation_reservations__connectionId__get`, which is ugly.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_get_reservations.py`:

```python
def test_reservation_get_routes_have_explicit_operation_ids() -> None:
    """Operation IDs are used as MCP component names and must be stable, clean strings."""
    schema = app.openapi()
    list_op = schema["paths"]["/reservations"]["get"]["operationId"]
    detail_op = schema["paths"]["/reservations/{connectionId}"]["get"]["operationId"]
    assert list_op == "list_reservations"
    assert detail_op == "get_reservation"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_get_reservations.py::test_reservation_get_routes_have_explicit_operation_ids -v`

Expected: FAIL — assertion on the auto-generated operation ID like `list_reservations_reservations_get`.

- [ ] **Step 3: Add operation_id to both GET decorators**

In `aggregator_proxy/routers/reservations.py`, find the two GET routes and add `operation_id`:

```python
@router.get(
    "/{connectionId}",
    response_model=ReservationDetail,
    summary="Get reservation details",
    operation_id="get_reservation",
)
async def get_reservation(...):
    ...


@router.get(
    "",
    response_model=ReservationsListResponse,
    summary="List all reservations",
    operation_id="list_reservations",
)
async def list_reservations(...):
    ...
```

Leave the function bodies completely unchanged.

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_get_reservations.py -v`

Expected: all PASS (including the new assertion and all existing get-reservation tests, since handler logic is unchanged).

- [ ] **Step 5: Commit**

```bash
git add aggregator_proxy/routers/reservations.py tests/test_get_reservations.py
git commit -m "Set explicit operation_id on GET /reservations routes"
```

---

## Task 4: Create `build_mcp` factory with route maps

**Files:**
- Create: `aggregator_proxy/mcp_server.py`
- Test: `tests/test_mcp_routes.py`

This task gets the route filtering right with **no auth wiring yet** (auth is layered in Tasks 5 and 6). It establishes the file and proves the route-map logic.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_routes.py`:

```python
# Copyright 2026 SURF
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests that build_mcp exposes only the two GET /reservations operations."""

from __future__ import annotations

import pytest
from fastmcp import Client

from aggregator_proxy.main import app
from aggregator_proxy.mcp_server import build_mcp


@pytest.fixture()
def _mcp_disabled_auth(monkeypatch) -> None:
    """Ensure auth flags are off so build_mcp can be constructed without OIDC config."""
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "mcp_auth_enabled", False)


async def test_only_get_reservations_are_exposed(_mcp_disabled_auth) -> None:
    mcp = build_mcp(app)
    async with Client(mcp) as client:
        resources = await client.list_resources()
        templates = await client.list_resource_templates()
        tools = await client.list_tools()

    resource_names = {r.name for r in resources}
    template_names = {t.name for t in templates}
    tool_names = {t.name for t in tools}

    assert resource_names == {"list_reservations"}
    assert template_names == {"get_reservation"}
    assert tool_names == set(), f"expected no tools but got: {tool_names}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_routes.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'aggregator_proxy.mcp_server'`.

- [ ] **Step 3: Create `aggregator_proxy/mcp_server.py`**

```python
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


"""FastMCP sub-app builder exposing GET /reservations as MCP Resources."""

from __future__ import annotations

from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType, RouteMap

_DETAIL_PATTERN = r"^/reservations/\{[^}]+\}$"
_LIST_PATTERN = r"^/reservations$"


def build_mcp(api: FastAPI) -> FastMCP:
    """Build a FastMCP server from the given FastAPI app, exposing only GET /reservations."""
    return FastMCP.from_fastapi(
        app=api,
        name="NSI Aggregator Proxy",
        route_maps=[
            RouteMap(methods=["GET"], pattern=_DETAIL_PATTERN, mcp_type=MCPType.RESOURCE_TEMPLATE),
            RouteMap(methods=["GET"], pattern=_LIST_PATTERN, mcp_type=MCPType.RESOURCE),
            RouteMap(mcp_type=MCPType.EXCLUDE),
        ],
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_routes.py -v`

Expected: PASS. If the test fails because `Client(mcp).list_resources()` returns a wrapper object with a different shape, adjust the assertions — but the wrapper API documented in FastMCP 3.x returns `list[Resource]` with `.name` attributes.

- [ ] **Step 5: Commit**

```bash
git add aggregator_proxy/mcp_server.py tests/test_mcp_routes.py
git commit -m "Add build_mcp factory with route maps for read-only reservations"
```

---

## Task 5: Add token forwarding event hook

**Files:**
- Modify: `aggregator_proxy/mcp_server.py`
- Test: `tests/test_mcp_auth.py` (new file)

When `auth_enabled=true`, the MCP-internal call must forward the MCP client's `Authorization` header so the existing `get_authenticated_user` dependency on `/reservations` re-validates.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_auth.py`:

```python
# Copyright 2026 SURF
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for MCP authentication wiring."""

from __future__ import annotations

import pytest

from aggregator_proxy.main import app


async def test_event_hook_registered_when_auth_enabled(monkeypatch) -> None:
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


async def test_no_event_hook_when_auth_disabled(monkeypatch) -> None:
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_auth.py -v`

Expected: FAIL — the spy captures empty `httpx_client_kwargs`.

- [ ] **Step 3: Add token forwarding to `build_mcp`**

Update `aggregator_proxy/mcp_server.py`:

```python
"""FastMCP sub-app builder exposing GET /reservations as MCP Resources."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.server.context import request_ctx
from fastmcp.server.providers.openapi import MCPType, RouteMap

from aggregator_proxy.settings import settings

_DETAIL_PATTERN = r"^/reservations/\{[^}]+\}$"
_LIST_PATTERN = r"^/reservations$"


async def _forward_user_token(request: httpx.Request) -> None:
    """Copy the MCP client's Authorization header onto the internal /reservations call."""
    ctx = request_ctx.get(None)
    if ctx is None:
        return
    incoming = getattr(ctx, "request", None)
    if incoming is None:
        return
    token = incoming.headers.get("authorization")
    if token:
        request.headers["Authorization"] = token


def build_mcp(api: FastAPI) -> FastMCP:
    """Build a FastMCP server from the given FastAPI app, exposing only GET /reservations."""
    httpx_kwargs: dict[str, Any] = {}
    if settings.auth_enabled:
        httpx_kwargs["event_hooks"] = {"request": [_forward_user_token]}

    return FastMCP.from_fastapi(
        app=api,
        name="NSI Aggregator Proxy",
        route_maps=[
            RouteMap(methods=["GET"], pattern=_DETAIL_PATTERN, mcp_type=MCPType.RESOURCE_TEMPLATE),
            RouteMap(methods=["GET"], pattern=_LIST_PATTERN, mcp_type=MCPType.RESOURCE),
            RouteMap(mcp_type=MCPType.EXCLUDE),
        ],
        httpx_client_kwargs=httpx_kwargs,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_auth.py tests/test_mcp_routes.py -v`

Expected: all PASS — the route-filtering test from Task 4 still passes (no behavioral change when auth is off), and both new auth-hook tests pass.

- [ ] **Step 5: Commit**

```bash
git add aggregator_proxy/mcp_server.py tests/test_mcp_auth.py
git commit -m "Forward MCP client Authorization header to internal /reservations call"
```

---

## Task 6: Add MCP-level OIDC authentication

**Files:**
- Modify: `aggregator_proxy/mcp_server.py`
- Test: `tests/test_mcp_auth.py` (append)

When `mcp_auth_enabled=true`, the MCP server itself must validate the incoming JWT (independently of whatever forwarding happens to the internal call).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_auth.py`:

```python
async def test_mcp_has_auth_provider_when_mcp_auth_enabled(monkeypatch) -> None:
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


async def test_mcp_has_no_auth_provider_when_mcp_auth_disabled(monkeypatch) -> None:
    from aggregator_proxy import mcp_server
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "mcp_auth_enabled", False)
    monkeypatch.setattr(settings, "auth_enabled", False)

    mcp = mcp_server.build_mcp(app)

    assert mcp.auth is None
```

(Note: `mcp.auth` is the documented attribute on FastMCP. If the attribute name differs in the installed version, adjust both assertions.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_auth.py -v -k auth_provider`

Expected: FAIL — `mcp.auth` is `None` even when `mcp_auth_enabled=True`.

- [ ] **Step 3: Wire OIDC auth in `build_mcp`**

Update `aggregator_proxy/mcp_server.py` (add the auth construction):

```python
"""FastMCP sub-app builder exposing GET /reservations as MCP Resources."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.server.auth import OAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.context import request_ctx
from fastmcp.server.providers.openapi import MCPType, RouteMap

from aggregator_proxy.settings import settings

_DETAIL_PATTERN = r"^/reservations/\{[^}]+\}$"
_LIST_PATTERN = r"^/reservations$"


async def _forward_user_token(request: httpx.Request) -> None:
    """Copy the MCP client's Authorization header onto the internal /reservations call."""
    ctx = request_ctx.get(None)
    if ctx is None:
        return
    incoming = getattr(ctx, "request", None)
    if incoming is None:
        return
    token = incoming.headers.get("authorization")
    if token:
        request.headers["Authorization"] = token


def _build_auth() -> OAuthProvider | None:
    """Build an MCP-level OIDC auth provider, or None if MCP auth is disabled."""
    if not settings.mcp_auth_enabled:
        return None
    verifier = JWTVerifier(
        jwks_uri=settings.oidc_jwks_uri,
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
    )
    return OAuthProvider(token_verifier=verifier)


def build_mcp(api: FastAPI) -> FastMCP:
    """Build a FastMCP server from the given FastAPI app, exposing only GET /reservations."""
    httpx_kwargs: dict[str, Any] = {}
    if settings.auth_enabled:
        httpx_kwargs["event_hooks"] = {"request": [_forward_user_token]}

    return FastMCP.from_fastapi(
        app=api,
        name="NSI Aggregator Proxy",
        auth=_build_auth(),
        route_maps=[
            RouteMap(methods=["GET"], pattern=_DETAIL_PATTERN, mcp_type=MCPType.RESOURCE_TEMPLATE),
            RouteMap(methods=["GET"], pattern=_LIST_PATTERN, mcp_type=MCPType.RESOURCE),
            RouteMap(mcp_type=MCPType.EXCLUDE),
        ],
        httpx_client_kwargs=httpx_kwargs,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_auth.py tests/test_mcp_routes.py -v`

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add aggregator_proxy/mcp_server.py tests/test_mcp_auth.py
git commit -m "Wire OIDC JWT validation for MCP endpoint when mcp_auth_enabled=true"
```

---

## Task 7: Mount MCP sub-app and add startup validation

**Files:**
- Modify: `aggregator_proxy/main.py`
- Test: `tests/test_mcp_auth.py` (append)

Mount the MCP sub-app on the FastAPI app, combine lifespans, and add the two startup-time validations.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_auth.py`:

```python
def test_startup_rejects_auth_enabled_without_mcp_auth(monkeypatch) -> None:
    """If REST auth is on but MCP auth is off, startup must refuse."""
    import importlib

    monkeypatch.setenv("AGGREGATOR_PROXY_MCP_ENABLED", "true")
    monkeypatch.setenv("AGGREGATOR_PROXY_AUTH_ENABLED", "true")
    monkeypatch.setenv("AGGREGATOR_PROXY_MCP_AUTH_ENABLED", "false")
    monkeypatch.setenv("AGGREGATOR_PROXY_OIDC_ISSUER", "https://idp.example.com")

    from aggregator_proxy import main, settings as settings_mod
    importlib.reload(settings_mod)
    with pytest.raises(SystemExit):
        importlib.reload(main)


def test_startup_rejects_mcp_auth_without_explicit_jwks_uri(monkeypatch) -> None:
    """MCP auth requires explicit OIDC_JWKS_URI (no lifespan auto-discovery available at module load)."""
    import importlib

    monkeypatch.setenv("AGGREGATOR_PROXY_MCP_ENABLED", "true")
    monkeypatch.setenv("AGGREGATOR_PROXY_AUTH_ENABLED", "true")
    monkeypatch.setenv("AGGREGATOR_PROXY_MCP_AUTH_ENABLED", "true")
    monkeypatch.setenv("AGGREGATOR_PROXY_OIDC_ISSUER", "https://idp.example.com")
    monkeypatch.delenv("AGGREGATOR_PROXY_OIDC_JWKS_URI", raising=False)

    from aggregator_proxy import main, settings as settings_mod
    importlib.reload(settings_mod)
    with pytest.raises(SystemExit):
        importlib.reload(main)


def test_mcp_path_returns_404_when_disabled(monkeypatch) -> None:
    """When mcp_enabled=false, the /mcp path is not mounted."""
    from fastapi.testclient import TestClient

    from aggregator_proxy.main import app
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "mcp_enabled", False)

    with TestClient(app) as client:
        response = client.get("/mcp")
    assert response.status_code == 404
```

Note: the two startup-rejection tests use `importlib.reload` to re-run `aggregator_proxy.main` with new env vars. This is somewhat coupled to module-load behavior — if module reload turns out unreliable, an alternative is to factor the validation into a `validate_settings()` function in `main.py` and call it directly. Either approach is acceptable; the test simply needs to exercise both rejection paths.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_auth.py -v -k "startup or 404"`

Expected: FAIL — `SystemExit` not raised; `/mcp` returns 404 only because nothing is mounted (this third test may already pass coincidentally, in which case keep it as a regression guard).

- [ ] **Step 3: Add startup validation and mount logic to `aggregator_proxy/main.py`**

At the bottom of `aggregator_proxy/main.py`, after the existing `app.include_router(...)` calls but before `def run()`, add:

```python
from fastmcp.utilities.lifespan import combine_lifespans  # noqa: E402

from aggregator_proxy.mcp_server import build_mcp  # noqa: E402

if settings.mcp_enabled:
    if settings.auth_enabled and not settings.mcp_auth_enabled:
        raise SystemExit(
            "AGGREGATOR_PROXY_MCP_AUTH_ENABLED must be true when AGGREGATOR_PROXY_AUTH_ENABLED is true"
        )
    if settings.mcp_auth_enabled and not settings.oidc_jwks_uri:
        raise SystemExit(
            "AGGREGATOR_PROXY_OIDC_JWKS_URI must be set explicitly when MCP_AUTH_ENABLED is true"
        )

    mcp_app = build_mcp(app).http_app(path="/")
    original_lifespan = app.router.lifespan_context
    app.router.lifespan_context = combine_lifespans(original_lifespan, mcp_app.lifespan)
    app.mount(settings.mcp_path, mcp_app)
```

The `# noqa: E402` comments are because these imports are placed after module-level code, which ruff would otherwise flag.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_auth.py tests/test_mcp_routes.py -v`

Expected: all PASS. Then run the full suite to check no regressions:

Run: `uv run pytest -v`

Expected: every existing test still passes. The MCP code only runs when `settings.mcp_enabled=true`, which is `False` by default in tests (conftest doesn't set it).

- [ ] **Step 5: Commit**

```bash
git add aggregator_proxy/main.py tests/test_mcp_auth.py
git commit -m "Mount MCP sub-app and add startup validation for auth coupling"
```

---

## Task 8: End-to-end integration test

**Files:**
- Create: `tests/test_mcp_integration.py`

Read both resources via `fastmcp.Client` against an in-process FastAPI app with a populated reservation store and mocked aggregator.

- [ ] **Step 1: Write the integration test**

Create `tests/test_mcp_integration.py`:

```python
# Copyright 2026 SURF
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""End-to-end MCP integration tests against the FastAPI app with a mocked aggregator."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from fastmcp import Client

from aggregator_proxy.main import app
from aggregator_proxy.mcp_server import build_mcp
from aggregator_proxy.nsi_soap import parse_correlation_id
from aggregator_proxy.reservation_store import ReservationStore
from tests.conftest import (
    build_query_notification_sync_response,
    build_query_summary_sync_response,
    make_reservation,
)


CONNECTION_ID = "conn-int-001"


def _mock_aggregator(connection_id: str) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        cid = parse_correlation_id(request.content)
        body = request.content.decode()
        if "queryNotificationSync" in body:
            return httpx.Response(200, content=build_query_notification_sync_response(cid))
        return httpx.Response(
            200,
            content=build_query_summary_sync_response(
                connection_id=connection_id, correlation_id=cid
            ),
        )

    return handler


@pytest.fixture()
def _app_with_reservation(monkeypatch) -> None:
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "mcp_auth_enabled", False)

    store = ReservationStore()
    store.create(make_reservation(connection_id=CONNECTION_ID, description="integration test"))
    app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_aggregator(CONNECTION_ID)))
    app.state.callback_client = httpx.AsyncClient()
    app.state.reservation_store = store


async def test_list_reservations_via_mcp(_app_with_reservation) -> None:
    mcp = build_mcp(app)
    async with Client(mcp) as client:
        # Discover the actual resource URI from the server rather than hardcoding it.
        resources = await client.list_resources()
        list_resource = next(r for r in resources if r.name == "list_reservations")
        contents = await client.read_resource(list_resource.uri)

    payload = json.loads(contents[0].text)
    assert "reservations" in payload
    ids = [r["connectionId"] for r in payload["reservations"]]
    assert CONNECTION_ID in ids


async def test_get_reservation_via_mcp(_app_with_reservation) -> None:
    mcp = build_mcp(app)
    async with Client(mcp) as client:
        templates = await client.list_resource_templates()
        get_template = next(t for t in templates if t.name == "get_reservation")
        # uriTemplate has the form `.../{connectionId}` — fill it in.
        uri = get_template.uriTemplate.replace("{connectionId}", CONNECTION_ID)
        contents = await client.read_resource(uri)

    payload = json.loads(contents[0].text)
    assert payload["connectionId"] == CONNECTION_ID
    assert payload["description"] == "integration test"


async def test_get_reservation_unknown_id_errors(_app_with_reservation) -> None:
    """A missing connection ID surfaces as an MCP error (not a silent empty result)."""
    mcp = build_mcp(app)
    async with Client(mcp) as client:
        templates = await client.list_resource_templates()
        get_template = next(t for t in templates if t.name == "get_reservation")
        uri = get_template.uriTemplate.replace("{connectionId}", "does-not-exist")
        with pytest.raises(Exception) as excinfo:
            await client.read_resource(uri)
    # The aggregator mock returns empty, so the underlying call yields 404.
    # FastMCP wraps non-2xx in an error containing the status code or detail.
    msg = str(excinfo.value)
    assert "404" in msg or "not found" in msg.lower()
```

Note: `Resource` and `ResourceTemplate` field names (`uri`, `uriTemplate`) match the MCP protocol. If the installed FastMCP version exposes them under different attribute names (e.g., `uri_template`), adjust accordingly — the test discovers everything by inspection so a single attribute rename is the only fix needed.

- [ ] **Step 2: Run the integration tests**

Run: `uv run pytest tests/test_mcp_integration.py -v`

Expected: PASS for `test_list_reservations_via_mcp` and `test_get_reservation_via_mcp`. The unknown-id test may need URI-pattern or error-message tuning.

- [ ] **Step 3: Run the full suite to confirm no regressions**

Run: `uv run pytest -v`

Expected: all pass.

- [ ] **Step 4: Run type check**

Run: `uv run mypy aggregator_proxy`

Expected: no errors. If `fastmcp` types are missing, add `fastmcp` to the `[[tool.mypy.overrides]]` `ignore_missing_imports` block in `pyproject.toml` (already global-ignored, so this should be fine).

- [ ] **Step 5: Run linter**

Run: `uv run ruff check . && uv run ruff format --check .`

Expected: no issues.

- [ ] **Step 6: Commit**

```bash
git add tests/test_mcp_integration.py
git commit -m "Add end-to-end MCP integration tests for resource reads"
```

---

## Task 9: Update README and CLAUDE.md

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

Document the new feature and env vars.

- [ ] **Step 1: Add MCP section to `README.md`**

Insert a new `## MCP Endpoint (optional)` section after the Authentication section (search for `## API Endpoints` and place the MCP section directly before it):

````markdown
## MCP Endpoint (optional)

The Aggregator Proxy can expose its read-only reservation endpoints as a [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server, mounted at `/mcp`. This lets AI agents (Claude Desktop, custom agents using `fastmcp.Client`, etc.) list and inspect reservations as MCP **Resources**.

Only the two GET operations are exposed:

- `GET /reservations` → MCP Resource `resource://list_reservations`
- `GET /reservations/{connectionId}` → MCP ResourceTemplate `resource://get_reservation/{connectionId}`

All state-changing operations (POST, DELETE) and the NSI callback endpoint are explicitly excluded from MCP.

### Configuration

| Variable | Default | Description |
|---|---|---|
| `AGGREGATOR_PROXY_MCP_ENABLED` | `false` | Mount the MCP sub-app. Opt-in; the feature is disabled by default. |
| `AGGREGATOR_PROXY_MCP_PATH` | `/mcp` | Mount path for the MCP sub-app. |
| `AGGREGATOR_PROXY_MCP_AUTH_ENABLED` | `false` | Require an OIDC JWT on the MCP endpoint. Validated by FastMCP's `JWTVerifier` using the same `OIDC_*` settings as the REST endpoints. |

**Startup validation:** when `AUTH_ENABLED=true`, `MCP_AUTH_ENABLED` must also be `true` — otherwise the proxy would expose authenticated data via an unauthenticated MCP endpoint. The proxy refuses to start in that configuration. Additionally, when `MCP_AUTH_ENABLED=true`, `OIDC_JWKS_URI` must be set explicitly (auto-discovery is not available at module load time).

### Minimal client example

```python
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

transport = StreamableHttpTransport(
    url="https://proxy.example.com/mcp/",
    headers={"Authorization": "Bearer <your-token>"},
)

async with Client(transport) as client:
    contents = await client.read_resource("resource://list_reservations")
    print(contents[0].text)
```
````

- [ ] **Step 2: Add a paragraph to `CLAUDE.md`**

In `CLAUDE.md`, under the "Key design points" section, append the following bullet after the existing "Dual-ingress authentication" bullet:

```markdown
- **Optional MCP sub-app**: when `MCP_ENABLED=true`, an `aggregator_proxy/mcp_server.py` factory builds a `FastMCP.from_fastapi()` sub-app mounted at `/mcp`. Route maps expose only `GET /reservations` (Resource) and `GET /reservations/{connectionId}` (ResourceTemplate); everything else is `MCPType.EXCLUDE`. The MCP-internal call to `/reservations` runs through the existing FastAPI handlers and dependencies, so no business logic is duplicated. When `auth_enabled=true`, an httpx event hook forwards the MCP client's `Authorization` header to the internal call so the existing `get_authenticated_user` dependency re-validates.
```

- [ ] **Step 3: Add the three new env vars to the CLAUDE.md configuration table**

In `CLAUDE.md`'s "Configuration reference" table, add three rows (place them at the end of the table):

```markdown
| `AGGREGATOR_PROXY_MCP_ENABLED` | No | `false` | Mount the MCP sub-app at `MCP_PATH`. Off by default; opt-in. |
| `AGGREGATOR_PROXY_MCP_PATH` | No | `/mcp` | Mount path for the MCP sub-app. |
| `AGGREGATOR_PROXY_MCP_AUTH_ENABLED` | No | `false` | Require an OIDC JWT on the MCP endpoint. Validated by FastMCP's `JWTVerifier` using the same `OIDC_*` settings. Must be `true` whenever `AUTH_ENABLED=true`. |
```

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "Document MCP endpoint configuration and usage"
```

---

## Final verification

After all tasks are complete:

- [ ] Run the full test suite: `uv run pytest -v`
- [ ] Run type check: `uv run mypy aggregator_proxy`
- [ ] Run lint: `uv run ruff check . && uv run ruff format --check .`
- [ ] Sanity-start the server with MCP enabled:

```bash
AGGREGATOR_PROXY_PROVIDER_URL=https://aggregator.example.com/nsi-v2/ConnectionServiceProvider \
  AGGREGATOR_PROXY_REQUESTER_NSA=urn:ogf:network:example.com:2025:requester-nsa \
  AGGREGATOR_PROXY_PROVIDER_NSA=urn:ogf:network:example.com:2025:provider-nsa \
  AGGREGATOR_PROXY_BASE_URL=https://proxy.example.com \
  AGGREGATOR_PROXY_MCP_ENABLED=true \
  uv run aggregator-proxy
```

Then in another shell, verify the MCP endpoint responds (an unauthenticated `GET /mcp/` should return an MCP error or info response — at minimum it shouldn't 404):

```bash
curl -i http://localhost:8080/mcp/
```

Expected: HTTP 200 (FastMCP info) or a documented MCP error — anything except 404 (which would mean the sub-app didn't mount).

Stop the server with Ctrl-C.

- [ ] Confirm the startup-validation paths by running the server with bad flags:

```bash
AGGREGATOR_PROXY_PROVIDER_URL=... \
  AGGREGATOR_PROXY_REQUESTER_NSA=... \
  AGGREGATOR_PROXY_PROVIDER_NSA=... \
  AGGREGATOR_PROXY_BASE_URL=... \
  AGGREGATOR_PROXY_AUTH_ENABLED=true \
  AGGREGATOR_PROXY_OIDC_ISSUER=https://idp.example.com \
  AGGREGATOR_PROXY_MCP_ENABLED=true \
  AGGREGATOR_PROXY_MCP_AUTH_ENABLED=false \
  uv run aggregator-proxy
```

Expected: the server exits immediately with the error `AGGREGATOR_PROXY_MCP_AUTH_ENABLED must be true when AGGREGATOR_PROXY_AUTH_ENABLED is true`.
