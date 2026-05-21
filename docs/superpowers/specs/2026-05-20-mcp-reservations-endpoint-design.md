# MCP Endpoint for Reservations — Design

**Date:** 2026-05-20
**Status:** Approved for implementation planning
**Scope:** Add a Model Context Protocol (MCP) endpoint that exposes the two read-only `/reservations` operations of the NSI Aggregator Proxy to MCP clients (AI agents).

## Goal

Mount an MCP sub-app on the existing FastAPI application that exposes:

- `GET /reservations` as an MCP **Resource**
- `GET /reservations/{connectionId}` as an MCP **ResourceTemplate**

All other routes (POST create, POST provision, POST release, DELETE terminate, the NSI callback, and health) must be invisible to MCP clients.

Implemented using `FastMCP.from_fastapi()` with route maps, so the existing FastAPI handlers and their dependencies remain the single source of truth.

## Non-goals

- No state-changing MCP tools (no provision, release, terminate, or create via MCP).
- No stdio transport. HTTP only.
- No MCP-specific rate limiting, audit logging beyond what already exists, or scope-based authorization beyond presence of a valid OIDC token.
- No changes to the simplified connection state machine, the SOAP layer, or the reservation store.

## Components

### New module — `aggregator_proxy/mcp_server.py`

A single factory `build_mcp(api: FastAPI) -> FastMCP` that returns a configured `FastMCP` instance with route maps that keep only the two GET reservation paths and exclude everything else.

```python
from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.server.auth import OAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.context import request_ctx
from fastmcp.server.providers.openapi import RouteMap, MCPType

from aggregator_proxy.settings import settings

_DETAIL_PATTERN = r"^/reservations/\{[^}]+\}$"
_LIST_PATTERN = r"^/reservations$"

async def _forward_user_token(request) -> None:
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
    auth = None
    if settings.mcp_auth_enabled:
        auth = OAuthProvider(
            token_verifier=JWTVerifier(
                jwks_uri=settings.oidc_jwks_uri,
                issuer=settings.oidc_issuer,
                audience=settings.oidc_audience,
            )
        )
    httpx_kwargs: dict = {}
    if settings.auth_enabled:
        httpx_kwargs["event_hooks"] = {"request": [_forward_user_token]}
    return FastMCP.from_fastapi(
        app=api,
        name="NSI Aggregator Proxy",
        auth=auth,
        route_maps=[
            RouteMap(methods=["GET"], pattern=_DETAIL_PATTERN, mcp_type=MCPType.RESOURCE_TEMPLATE),
            RouteMap(methods=["GET"], pattern=_LIST_PATTERN, mcp_type=MCPType.RESOURCE),
            RouteMap(mcp_type=MCPType.EXCLUDE),  # catch-all — hide everything else
        ],
        httpx_client_kwargs=httpx_kwargs,
    )
```

### Modified `aggregator_proxy/main.py`

Inside the existing `lifespan`, add the startup validation. After the existing `app.include_router(...)` calls, mount the MCP ASGI app:

```python
from fastmcp.utilities.lifespan import combine_lifespans
from aggregator_proxy.mcp_server import build_mcp

# Module level — startup-time validation (raised before uvicorn binds the port):
if settings.mcp_enabled:
    if settings.auth_enabled and not settings.mcp_auth_enabled:
        raise SystemExit(
            "AGGREGATOR_PROXY_MCP_AUTH_ENABLED must be true when AGGREGATOR_PROXY_AUTH_ENABLED is true"
        )
    if settings.mcp_auth_enabled and not settings.oidc_jwks_uri:
        raise SystemExit(
            "AGGREGATOR_PROXY_OIDC_JWKS_URI must be set explicitly when MCP_AUTH_ENABLED is true"
        )

# Module level — after include_router calls, before serving:
if settings.mcp_enabled:
    mcp_app = build_mcp(app).http_app(path="/")
    original_lifespan = app.router.lifespan_context
    app.router.lifespan_context = combine_lifespans(original_lifespan, mcp_app.lifespan)
    app.mount(settings.mcp_path, mcp_app)
```

Note: the `combine_lifespans` call replaces `lifespan_context` so the existing lifespan still runs; `mcp_app.lifespan` adds FastMCP's session-store startup/shutdown. Mutating `lifespan_context` after construction is the cleanest pattern given the chicken-and-egg between `FastMCP.from_fastapi(app)` needing the app and `FastAPI(lifespan=...)` needing the combined lifespan; an equivalent refactor that defers `FastAPI` construction until after MCP is built is acceptable too.

### Modified `aggregator_proxy/settings.py`

New settings (all optional, default off so the feature is opt-in):

```python
mcp_enabled: bool = False     # Mount the MCP sub-app
mcp_path: str = "/mcp"        # Mount path
mcp_auth_enabled: bool = False  # Require OIDC at the MCP layer
```

These follow the existing `AGGREGATOR_PROXY_` env-var convention.

### Modified `aggregator_proxy/routers/reservations.py`

Add explicit `operation_id` to the two GET routes so MCP component names are clean:

- `GET /reservations` → `operation_id="list_reservations"`
- `GET /reservations/{connectionId}` → `operation_id="get_reservation"`

No other handler changes.

### Modified `pyproject.toml`

Add `fastmcp` (latest stable 3.x) to `[project.dependencies]`. Pin to a specific minor version per the project's existing strict-pin style (e.g. `fastmcp==3.2.4`).

## Authentication model

| `auth_enabled` | `mcp_auth_enabled` | Behavior |
|---|---|---|
| false | false | Everything open. MCP and REST both unauthenticated. |
| false | true | MCP requires OIDC. Internal call to `/reservations` runs without auth (dependency is permissive). |
| true | true | MCP requires OIDC; user's `Authorization` header forwarded by the httpx event hook to the internal `/reservations` call. Existing `get_authenticated_user` dependency re-validates. |
| true | false | **Rejected at startup.** Would let MCP leak authenticated data. `SystemExit` raised. |

The forwarding hook is only registered when `auth_enabled=true`; when REST auth is off, no forwarding happens (no header to forward, no need to add headers).

OIDC settings (`oidc_issuer`, `oidc_audience`, `oidc_jwks_uri`) are reused — there is one OIDC identity provider for the whole proxy. We do not introduce a separate OIDC config for MCP.

**Constraint: explicit `oidc_jwks_uri` required when `mcp_auth_enabled=true`.** The existing OIDC auto-discovery runs inside `lifespan` (after `httpx.AsyncClient` is available), but the MCP server is constructed at module load — before any discovery has happened. To avoid duplicating the discovery logic or delaying MCP mount, we require `AGGREGATOR_PROXY_OIDC_JWKS_URI` to be set explicitly when `mcp_auth_enabled=true`. Validated at startup with a clear `SystemExit` if missing. (The REST-side auth retains its existing auto-discovery behavior unchanged.)

## MCP component naming

| FastAPI route | MCP component | URI / Name |
|---|---|---|
| `GET /reservations` | Resource | `resource://list_reservations` (name from `operation_id`) |
| `GET /reservations/{connectionId}` | ResourceTemplate | `resource://get_reservation/{connectionId}` |

The existing `detail` query parameter (`summary` \| `full` \| `recursive`) is preserved automatically by `from_fastapi`, derived from the route's signature.

## Errors

`aggregator_error_handler` (502 on `httpx.HTTPStatusError`) is already registered on the FastAPI app and fires on the MCP-internal call. FastMCP surfaces non-2xx responses from the internal client as MCP protocol errors with the JSON body as the message. No new error handling is required.

Specifically:
- 404 from `GET /reservations/{connectionId}` → MCP resource read error with the JSON `{"detail": "..."}` body
- 502 (aggregator unreachable) → MCP error surfacing the proxy's 502 body
- 400 (`detail=recursive` on list) → MCP error (list endpoint rejects recursive)

## Testing

Three new test files, all using the existing pytest setup (`pytest-asyncio` auto mode + `pytest-httpx` for mocking outbound NSI calls):

### `tests/test_mcp_routes.py`
- After `build_mcp(app)`, assert the resource list contains exactly `list_reservations`
- Assert the resource-template list contains exactly `get_reservation`
- Assert the tool list is empty (no POST/DELETE exposed)

### `tests/test_mcp_integration.py`
- Spin up the FastAPI app with `mcp_enabled=true`, populate the in-memory store via `make_reservation`, mock the aggregator's `querySummarySync`/`queryNotificationSync` with `pytest-httpx`
- Use `fastmcp.Client` against an in-process ASGI transport; call `list_resources()`, `read_resource("resource://list_reservations")`, and `read_resource("resource://get_reservation/<id>")`
- Verify the `detail=full` parameter works and returns segments
- Verify a non-existent connection ID surfaces as an MCP error

### `tests/test_mcp_auth.py`
- Startup-validation: `auth_enabled=true, mcp_auth_enabled=false, mcp_enabled=true` raises `SystemExit`
- Startup-validation: `mcp_auth_enabled=true, oidc_jwks_uri=""` raises `SystemExit`
- Token-forwarding: with both flags on, a request to MCP with a Bearer token is propagated to the internal `/reservations` call (assert via a captured request on the httpx mock)
- No-MCP: with `mcp_enabled=false`, the `/mcp` path returns 404 (sub-app not mounted)

## Documentation

- **`README.md`**: add a short MCP section after the auth section, describing the two new env vars and showing a minimal `fastmcp.Client` example
- **`CLAUDE.md`**: add a short paragraph in the "Key design points" section noting the optional MCP sub-app and how route maps filter it to read-only reservations endpoints
- **`chart/values.yaml`**: no schema change needed — the existing `env` and `envFromSecret` map already supports `AGGREGATOR_PROXY_MCP_*` variables; add commented examples

## Sequence diagram — authenticated MCP read

```
AI Agent           MCP layer (FastMCP)        FastAPI /reservations          Aggregator
  |                       |                            |                          |
  |--read_resource------->|                            |                          |
  |  + Bearer JWT         |                            |                          |
  |                       |--validate JWT (JWKS)------>|                          |
  |                       |   (JWTVerifier)            |                          |
  |                       |--internal httpx call------>|                          |
  |                       |  + forwarded Authorization |                          |
  |                       |                            |--get_authenticated_user->|
  |                       |                            |  (re-validates same JWT) |
  |                       |                            |--querySummarySync------->|
  |                       |                            |<-querySummarySyncConfirmed
  |                       |                            |--queryNotificationSync-->|
  |                       |                            |<-queryNotificationSyncConfirmed
  |                       |<--ReservationDetail JSON---|                          |
  |<--Resource contents---|                            |                          |
```

## Open questions / future work

- **Resource updates / subscriptions** — MCP supports resource subscriptions (notifications on change). The current design does not implement these; a future iteration could push notifications when the in-memory store changes.
- **Tool exposure for state-changing ops** — explicitly out of scope for this design; if requested later, would require a new design pass on async-callback handling (the 202 + callback pattern doesn't map cleanly to synchronous MCP tools).
- **Per-tool/per-resource scopes** — `mcp_auth_enabled` is binary today. Could later add scope checks (e.g., `read:reservations`) via FastMCP's `require_scopes` decorator.

## Decision log

| Decision | Choice | Rationale |
|---|---|---|
| MCP scope | Read-only (the two GETs) | User explicitly requested read-only |
| Library | Official `fastmcp` via `from_fastapi()` with route maps | User specified; minimal hand-written glue; reuses existing FastAPI handlers as the source of truth |
| Resource vs Tool | Resource + ResourceTemplate | MCP-idiomatic for GET endpoints; future-proof if subscriptions added |
| Auth coupling | "MCP stricter only" with token forwarding | Avoids introducing a new internal credential type; existing auth model unchanged |
| Mount path | `/mcp` (configurable via `mcp_path`) | Conventional MCP path; configurable for proxy/ingress flexibility |
| Default state | `mcp_enabled=false` | Opt-in; no behavior change for existing deployments |
