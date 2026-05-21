# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

This project uses [uv](https://docs.astral.sh/uv/) for dependency and environment management.

```bash
# Install dependencies
uv sync

# Run the application (requires all four AGGREGATOR_PROXY_* variables below)
AGGREGATOR_PROXY_PROVIDER_URL=https://aggregator.example.com/nsi-v2/ConnectionServiceProvider \
  AGGREGATOR_PROXY_REQUESTER_NSA=urn:ogf:network:example.com:2025:requester-nsa \
  AGGREGATOR_PROXY_PROVIDER_NSA=urn:ogf:network:example.com:2025:provider-nsa \
  AGGREGATOR_PROXY_BASE_URL=https://proxy.example.com \
  uv run aggregator-proxy

# Run tests
uv run pytest

# Run a single test
uv run pytest tests/path/to/test_file.py::test_function_name

# Lint
uv run ruff check .

# Format
uv run ruff format .

# Type check
uv run mypy aggregator_proxy

# Build Docker image
docker build -t nsi-aggregator-proxy .
```

## Architecture

This is a **FastAPI** application that exposes a simplified REST API on top of an NSI (Network Service Interface) aggregator (e.g. Safnari). Instead of the complex multi-state-machine NSI protocol, it presents a single simplified connection state machine with states: `RESERVING` → `RESERVED` → `ACTIVATING` → `ACTIVATED` → `DEACTIVATING` → back to `RESERVED`, plus `FAILED` and `TERMINATED`.

### Key design points

- **Async throughout**: uses `httpx.AsyncClient` for outbound calls to the NSI aggregator, FastAPI's async handlers, and uvicorn as the ASGI server.
- **mTLS support**: the `httpx.AsyncClient` (created in `nsi_client.py`) can be configured with a client certificate/key pair and a custom CA bundle for mutual TLS against the aggregator.
- **Shared client via app state**: the `httpx.AsyncClient` is created at startup in the `lifespan` context manager (`main.py`) and stored in `app.state.nsi_client`. Routers access it through the `get_nsi_client` FastAPI dependency (`dependencies.py`).
- **Structured logging**: all logging goes through `structlog` with a shared pipeline that also captures uvicorn's stdlib logs. `/health` endpoint access logs are suppressed. Configured in `logging_config.py`.
- **Settings**: all configuration is via environment variables with the `AGGREGATOR_PROXY_` prefix, managed by `pydantic-settings` (`settings.py`). The required variables are `AGGREGATOR_PROXY_PROVIDER_URL`, `AGGREGATOR_PROXY_REQUESTER_NSA`, `AGGREGATOR_PROXY_PROVIDER_NSA`, and `AGGREGATOR_PROXY_BASE_URL`.
- **Dual-ingress authentication**: when `AUTH_ENABLED=true`, every request to `/reservations` must be authenticated via OIDC (JWT) or mTLS (header from nsi-auth). OIDC is active when `OIDC_ISSUER` is set; mTLS is active when `MTLS_HEADER` is set. The `/health` endpoint is always unauthenticated. The `/nsi/v2/callback` endpoint requires mTLS (not OIDC) when auth is enabled and `MTLS_HEADER` is set — the aggregator is a machine client, not a browser user. OIDC discovery validates that both `jwks_uri` and `userinfo_endpoint` are available, failing fast at startup if not. Group-based authorization via userinfo endpoint. Separate vanilla httpx client for OIDC calls (not the mTLS NSI client). `OIDC_REQUIRED_GROUPS` must be `[]` (not empty string) when no groups are required.
- **Optional MCP sub-app**: when `MCP_ENABLED=true`, an `aggregator_proxy/mcp_server.py` factory builds a `FastMCP.from_fastapi()` sub-app mounted at `/mcp`. Route maps expose only `GET /reservations` (Resource) and `GET /reservations/{connectionId}` (ResourceTemplate); everything else is `MCPType.EXCLUDE`. The MCP-internal call to `/reservations` runs through the existing FastAPI handlers and dependencies, so no business logic is duplicated. When `auth_enabled=true`, an httpx event hook forwards the MCP client's `Authorization` header to the internal call so the existing `get_authenticated_user` dependency re-validates.

### Module layout

```
aggregator_proxy/
  main.py               # FastAPI app, lifespan, entry point (run()), /health endpoint
  settings.py           # pydantic-settings config (env prefix: AGGREGATOR_PROXY_)
  auth.py               # OIDC JWT + mTLS authentication (get_authenticated_user,
                        #   get_mtls_authenticated_callback dependencies)
  models.py             # Pydantic request/response models and ReservationStatus enum
  reservation_store.py  # In-memory reservation store and pending NSI correlation tracking
  state_mapping.py      # Maps NSI sub-state machines to proxy ReservationStatus
  nsi_client.py         # httpx.AsyncClient factory with mTLS config
  dependencies.py       # FastAPI dependency: get_nsi_client
  logging_config.py     # structlog + stdlib unified logging pipeline
  routers/
    reservations.py     # All /reservations endpoints (POST, GET, DELETE, provision, release)
    nsi_callback.py     # POST /nsi/v2/callback — receives async NSI callbacks
  nsi_soap/
    namespaces.py       # Shared NSMAP dict for all NSI CS v2 XML namespaces
    builder.py          # NsiHeader dataclass + build_reserve / build_reserve_commit /
                        #   build_provision / build_release / build_terminate /
                        #   build_query_summary_sync / build_query_notification_sync /
                        #   build_query_recursive (lxml)
    parser.py           # parse() dispatcher → typed dataclasses for every inbound
                        #   message type; includes ChildSegment (path segment data) and
                        #   QueryRecursiveResult; see module docstring for classification
```

### NSI SOAP layer

The `nsi_soap` package handles the translation between the REST layer and the NSI CS v2 SOAP protocol spoken by the aggregator (e.g. Safnari).

**Building requests** — pass an `NsiHeader` (requester/provider NSA URNs, replyTo URL, auto-generated correlationId) plus operation-specific arguments to the relevant `build_*` function; each returns UTF-8 XML bytes ready to POST.

**Parsing responses** — call `parse(xml)` on any received SOAP envelope. Both `parse()` and `parse_correlation_id()` accept `XmlInput` (either raw `bytes` or a pre-parsed `etree._Element`), so callers that already have a parsed tree can avoid a redundant `fromstring()`. The function returns one of the typed dataclasses below; use a `match` statement in the caller to handle each case.

| Dataclass | Sync/Async | Trigger |
|---|---|---|
| `ReserveResponse` | Sync | HTTP response to `reserve` |
| `Acknowledgment` | Sync | HTTP response to `provision`, `release`, `terminate` |
| `ReserveConfirmed` | Async callback | Reserve held, proxy must send `reserveCommit` |
| `ReserveFailed` | Async callback | State → FAILED; carries `ServiceException` (errorId + text per GFD.235) |
| `ReserveTimeout` | Async callback | State → FAILED; reserve timed out before commit |
| `ReserveCommitConfirmed` | Async callback | State → RESERVED |
| `ReserveCommitFailed` | Async callback | State → FAILED; carries `ServiceException` |
| `ProvisionConfirmed` | Async callback | Awaiting `dataPlaneStateChange` |
| `DataPlaneStateChange` | Async callback | `active=True` → ACTIVATED, `active=False` → RESERVED |
| `ReleaseConfirmed` | Async callback | State → RESERVED |
| `TerminateConfirmed` | Async callback | State → TERMINATED |
| `QueryRecursiveResult` | Async callback | Response to `queryRecursive`; carries `list[QueryReservation]` with `ChildSegment` children including per-segment `ConnectionStates` |

### Current implementation status

`POST /reservations`, `POST /reservations/{connectionId}/provision`, `POST /reservations/{connectionId}/release`, and `DELETE /reservations/{connectionId}` (terminate) are fully implemented. Reserve sends the NSI reserve request, waits for the async `reserveConfirmed` callback, sends `reserveCommit`, and delivers the final status via the caller's `callbackURL`. Provision sends the NSI provision request, waits for `provisionConfirmed`, then waits for `DataPlaneStateChange(active=True)` to transition to ACTIVATED. Release sends the NSI release request, waits for `releaseConfirmed`, then waits for `DataPlaneStateChange(active=False)` to transition back to RESERVED. Terminate sends the NSI terminate request, waits for `terminateConfirmed`, and transitions to TERMINATED (both success and timeout result in TERMINATED per the state machine).

**Aggregator state refresh**: Before every operation (except `POST /reservations` which creates a new reservation), the proxy queries the aggregator via `querySummarySync` and maps the NSI sub-state machines to the proxy state, and concurrently calls `queryNotificationSync` (via `asyncio.gather()`) to detect error events (`activateFailed`, `deactivateFailed`, `dataplaneError`, `forcedEnd`) that are not visible in the sub-state machines. This ensures the proxy's state reflects changes made outside the proxy (e.g. PassedEndTime, other NSI clients, error events). On startup, a full `querySummarySync` populates the store. The `requesterNSA` for query requests comes from `settings.requester_nsa`; for operation requests (reserve, commit, provision, release, terminate) it comes from the user-supplied `requesterNSA`. The `providerNSA` from `POST /reservations` is validated against `settings.provider_nsa` and rejected with 400 if they don't match.

**Detail query parameter**: `GET /reservations/{connectionId}` and `GET /reservations` accept a `detail` query parameter (`summary`, `full`, `recursive`) that controls path segment visibility. At `summary` (default) no segment data is returned. At `full`, the `querySummarySync` response's `<children>` elements are parsed into `segments[]` (order, connectionId, providerNSA, STPs, capacity) at no extra cost. At `recursive`, an async `queryRecursive` round-trip to the aggregator returns per-segment connection states mapped to proxy `status`. `detail=recursive` is rejected with 400 on the list endpoint because the async fan-out per reservation is too expensive. Segment data is transient (computed per request, not stored).

The state mapping module (`aggregator_proxy/state_mapping.py`) maps NSI sub-state machines to proxy states in priority order: Terminated/PassedEndTime → TERMINATED, Failed lifecycle → FAILED, ReserveTimeout/ReserveFailed/ReserveAborting → FAILED, has_error_event → FAILED, ReserveChecking/ReserveHeld/ReserveCommitting → RESERVING, Released + active → DEACTIVATING, dataPlane active → ACTIVATED, Provisioned → ACTIVATING, otherwise → RESERVED.

### Configuration reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `AGGREGATOR_PROXY_PROVIDER_URL` | Yes | — | Full URL of the NSI provider endpoint on the aggregator (e.g. `https://safnari.example.com/nsi-v2/ConnectionServiceProvider`) |
| `AGGREGATOR_PROXY_REQUESTER_NSA` | Yes | — | NSA URN used as requesterNSA in `querySummarySync` requests to the aggregator |
| `AGGREGATOR_PROXY_PROVIDER_NSA` | Yes | — | NSA URN of the aggregator; used as providerNSA in all outbound SOAP headers and validated against `providerNSA` in `POST /reservations` |
| `AGGREGATOR_PROXY_BASE_URL` | Yes | — | Externally reachable base URL of this proxy; `/nsi/v2/callback` is appended to form the `replyTo` in outbound SOAP headers |
| `AGGREGATOR_PROXY_CLIENT_CERT` | No | None | Path to client TLS certificate |
| `AGGREGATOR_PROXY_CLIENT_KEY` | No | None | Path to client TLS private key |
| `AGGREGATOR_PROXY_CA_FILE` | No | None | Path to CA bundle for server verification |
| `AGGREGATOR_PROXY_NSI_TIMEOUT` | No | `180` | Seconds to wait for async NSI callbacks (reserve, commit, provision, release, terminate) |
| `AGGREGATOR_PROXY_DATAPLANE_TIMEOUT` | No | `300` | Seconds to wait for `DataPlaneStateChange(active=True)` after provision |
| `AGGREGATOR_PROXY_LOG_LEVEL` | No | `INFO` | Log level |
| `AGGREGATOR_PROXY_HOST` | No | `0.0.0.0` | Bind host |
| `AGGREGATOR_PROXY_PORT` | No | `8080` | Bind port |
| `AGGREGATOR_PROXY_ROOT_PATH` | No | _(empty)_ | ASGI root path prefix for reverse proxy with path stripping |
| `AGGREGATOR_PROXY_AUTH_ENABLED` | No | `false` | Enable authentication on `/reservations` endpoints |
| `AGGREGATOR_PROXY_MTLS_HEADER` | No | _(empty)_ | Header name that nsi-auth sets on successful mTLS validation (e.g. `X-Auth-Method`) |
| `AGGREGATOR_PROXY_OIDC_ISSUER` | No | _(empty)_ | Expected `iss` claim in the JWT; OIDC is active when set |
| `AGGREGATOR_PROXY_OIDC_AUDIENCE` | No | _(empty)_ | Expected `aud` claim in the JWT |
| `AGGREGATOR_PROXY_OIDC_JWKS_URI` | No | _(empty)_ | JWKS endpoint URL; auto-discovered from issuer if empty |
| `AGGREGATOR_PROXY_OIDC_USERINFO_URI` | No | _(empty)_ | Userinfo endpoint URL; auto-discovered if empty |
| `AGGREGATOR_PROXY_OIDC_GROUP_CLAIM` | No | `eduperson_entitlement` | Claim name in userinfo containing group memberships |
| `AGGREGATOR_PROXY_OIDC_REQUIRED_GROUPS` | No | `[]` | Groups required for access (JSON array or comma-separated) |
| `AGGREGATOR_PROXY_OIDC_JWKS_CACHE_LIFESPAN` | No | `300` | JWKS key cache TTL in seconds |
| `AGGREGATOR_PROXY_OIDC_USERINFO_CACHE_TTL` | No | `60` | Userinfo response cache TTL in seconds |
| `AGGREGATOR_PROXY_MCP_ENABLED` | No | `false` | Mount the MCP sub-app at `MCP_PATH`. Off by default; opt-in. |
| `AGGREGATOR_PROXY_MCP_PATH` | No | `/mcp` | Mount path for the MCP sub-app. |
| `AGGREGATOR_PROXY_MCP_AUTH_ENABLED` | No | `false` | Require an OIDC JWT on the MCP endpoint. Validated by FastMCP's `JWTVerifier` using the issuer/audience/JWKS URI from the existing `OIDC_*` settings. Group-based authorization is not enforced on MCP. Must be `true` whenever `AUTH_ENABLED=true`. |

### Non-obvious invariants

- **Register pending future before sending SOAP**: in every operation that awaits an async callback (reserve, commit, provision, release, terminate, queryRecursive), `store.register_pending(correlation_id)` is called *before* the outbound SOAP POST. If the future were registered after, the callback could arrive and be dropped before the future exists.
- **`DataPlaneStateChange` dual resolution**: the callback router resolves this message via *both* `resolve_pending(correlation_id, ...)` and `resolve_pending_by_connection(connection_id, ...)`. The aggregator sends data-plane notifications with its own self-generated correlationId (not the correlationId used in the provision/release request), so the operation background tasks register a connection-keyed future (`register_pending_by_connection`) that `_await_dataplane_change` loops on.
- **Reservation store is in-memory only**: there is no database. On restart the store is repopulated from `querySummarySync` at startup, but in-flight `asyncio.Future` objects and pending correlation tracking are lost. Any operation that was waiting for a callback when the process restarted will never complete.

### Testing

Tests use `pytest-asyncio` in `auto` mode (all async test functions run automatically). Outbound HTTP calls are mocked with `pytest-httpx` (`respx`-style). The `tests/conftest.py` provides shared helpers:

- `build_query_summary_sync_response(connection_id, correlation_id, ...)` — SOAP `querySummarySyncConfirmed` response
- `build_query_notification_sync_response(correlation_id, *error_events)` — SOAP `queryNotificationSyncConfirmed` response
- `build_error_event_xml(...)` — `<errorEvent>` XML fragment
- `build_child_xml(...)` / `build_connection_states_xml(...)` — child segment XML fragments
- `build_query_summary_sync_response_with_children(...)` / `build_query_recursive_confirmed_response(...)` — responses with path children
- `build_soap_envelope(body_xml, correlation_id)` — wraps any body XML in a full SOAP envelope
- `build_acknowledgment_xml(correlation_id)` — NSI acknowledgment response
- `get_pending_correlation_id(store)` — extract the single pending correlationId from a store (asserts exactly one)
- `make_reservation(...)` — build a `Reservation` with sensible defaults
- `store` fixture — fresh `ReservationStore` per test

### Local env file

`aggregator_proxy.env` in the repo root can hold `AGGREGATOR_PROXY_*` values as `KEY=VALUE` lines. Environment variables take precedence. The file is read automatically on startup if present in the working directory.

### Code style

- Line length: 120 characters
- Docstrings: Google convention
- All functions must be fully type-annotated (mypy strict mode)
- Linter: ruff with rules A, B, C4, D, E, F, G, I, ISC, S, T20, W enabled
