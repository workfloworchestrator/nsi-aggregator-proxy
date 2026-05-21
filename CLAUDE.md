# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

This project uses [uv](https://docs.astral.sh/uv/) for dependency and environment management.

```bash
# Install dependencies
uv sync

# Run the application (requires all four variables below)
PROVIDER_URL=https://aggregator.example.com/nsi-v2/ConnectionServiceProvider \
  REQUESTER_NSA=urn:ogf:network:example.com:2025:requester-nsa \
  PROVIDER_NSA=urn:ogf:network:example.com:2025:provider-nsa \
  BASE_URL=https://proxy.example.com \
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
- **Settings**: all configuration is via environment variables (bare names, no prefix), managed by `pydantic-settings` (`settings.py`). The required variables are `PROVIDER_URL`, `REQUESTER_NSA`, `PROVIDER_NSA`, and `BASE_URL`.
- **Trusted-header authentication**: when `PROXY_AUTH_ENABLED=true`, every request to `/reservations`, `/openapi.json`, `/docs`, and `/redoc` must carry identity headers set by the edge proxy. On the portal route, Traefik plus oauth2-proxy lands `X-Auth-Request-Email` and `X-Auth-Request-Groups`. On the mTLS route, the `nsi-auth` validate sidecar lands the configured `MTLS_HEADER` plus `X-Client-DN`. `OIDC_REQUIRED_GROUPS` (a single list of group URNs) gates both surfaces; `check_groups` is a pure set intersection against the parsed `X-Auth-Request-Groups`. `/health` is always unauthenticated. `/nsi/v2/callback` uses a stricter dependency that accepts only the mTLS header (`get_mtls_authenticated_callback`) so browser/OIDC users can't forge async NSI callbacks even if Traefik routing puts them on a path that reaches it. `OIDC_REQUIRED_GROUPS` must be `[]` (not empty string) when no groups are required — pydantic-settings JSON-parses `list[str]` env vars before field validators run.
- **MCP as a local gateway**: when `MCP_ENABLED=true`, an `aggregator_proxy/mcp_server.py` factory builds a `FastMCP.from_fastapi()` sub-app mounted at `MCP_PATH`. Route maps expose only `GET /reservations` (Resource) and `GET /reservations/{connectionId}` (ResourceTemplate); everything else is `MCPType.EXCLUDE`. MCP keeps its own JWT verifier (`fastmcp.JWTVerifier`, configured via the dedicated `MCP_OIDC_*` settings) because MCP access tokens come from a different OIDC provider than the portal IdP. The internal MCP→REST call goes through an httpx event hook (`_forward_user_identity`) that decodes the validated JWT's payload, reads `MCP_OIDC_EMAIL_CLAIM` and `MCP_OIDC_GROUPS_CLAIM`, and sets `X-Auth-Request-Email` + `X-Auth-Request-Groups` on the outgoing request — letting REST trust the same kind of headers as the portal path. `Authorization` is dropped on the internal call. The Settings model validator refuses the combination `PROXY_AUTH_ENABLED=true ∧ MCP_ENABLED=true ∧ MCP_AUTH_ENABLED=false` at startup to prevent unverified JWT claims from being translated into trusted REST headers.

### Module layout

```
aggregator_proxy/
  main.py               # FastAPI app via create_app() factory, lifespan, entry point (run()),
                        #   /health, auth-gated /openapi.json /docs /redoc
  settings.py           # pydantic-settings config (no env prefix); cross-field model
                        #   validator forbids PROXY_AUTH_ENABLED + MCP_ENABLED + !MCP_AUTH_ENABLED
  auth.py               # Trusted-header authentication: get_authenticated_user reads
                        #   X-Auth-Request-Email/Groups (OIDC) or MTLS_HEADER (mTLS);
                        #   get_mtls_authenticated_callback is strict-mTLS-only
  mcp_server.py         # FastMCP factory + _forward_user_identity hook that translates
                        #   the validated MCP JWT into X-Auth-Request-* trusted headers
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
| `PROVIDER_URL` | Yes | — | Full URL of the NSI provider endpoint on the aggregator (e.g. `https://safnari.example.com/nsi-v2/ConnectionServiceProvider`) |
| `REQUESTER_NSA` | Yes | — | NSA URN used as requesterNSA in `querySummarySync` requests to the aggregator |
| `PROVIDER_NSA` | Yes | — | NSA URN of the aggregator; used as providerNSA in all outbound SOAP headers and validated against `providerNSA` in `POST /reservations` |
| `BASE_URL` | Yes | — | Externally reachable base URL of this proxy; `/nsi/v2/callback` is appended to form the `replyTo` in outbound SOAP headers |
| `CLIENT_CERT` | No | None | Path to client TLS certificate |
| `CLIENT_KEY` | No | None | Path to client TLS private key |
| `CA_FILE` | No | None | Path to CA bundle for server verification |
| `NSI_TIMEOUT` | No | `180` | Seconds to wait for async NSI callbacks (reserve, commit, provision, release, terminate) |
| `DATAPLANE_TIMEOUT` | No | `300` | Seconds to wait for `DataPlaneStateChange(active=True)` after provision |
| `LOG_LEVEL` | No | `INFO` | Log level |
| `HOST` | No | `0.0.0.0` | Bind host |
| `PORT` | No | `8080` | Bind port |
| `ROOT_PATH` | No | _(empty)_ | ASGI root path prefix for reverse proxy with path stripping |
| `PROXY_AUTH_ENABLED` | No | `false` | Enable authentication on `/reservations`, `/openapi.json`, `/docs`, and `/redoc`. `/health` stays unauthenticated. |
| `MTLS_HEADER` | No | _(empty)_ | Header name that nsi-auth sets on successful mTLS validation (e.g. `X-Auth-Method`). When set and auth is enabled, the presence of this header counts as mTLS authentication; `X-Client-DN` is logged for audit. |
| `OIDC_REQUIRED_GROUPS` | No | `[]` | Groups required for access (JSON array or comma-separated). Single list gates both the portal path and (when MCP is enabled) MCP-mediated calls, so it must include URNs from both providers. |
| `MCP_ENABLED` | No | `false` | Mount the MCP sub-app at `MCP_PATH`. Off by default; opt-in. |
| `MCP_PATH` | No | `/mcp` | Mount path for the MCP sub-app. Must start with `/` and not end with `/`; validated at startup. |
| `MCP_AUTH_ENABLED` | No | `false` | Validate incoming MCP JWTs via `fastmcp.JWTVerifier`. **Must be `true`** when `PROXY_AUTH_ENABLED=true` and `MCP_ENABLED=true`; the Settings model validator refuses the unsafe combination at startup to prevent the claim-translation hook from forwarding unverified claims as trusted headers. |
| `MCP_OIDC_JWKS_URI` | No | _(empty)_ | JWKS URI for the MCP OIDC provider (separate IdP from the portal). |
| `MCP_OIDC_ISSUER` | No | _(empty)_ | Expected `iss` claim for MCP-issued JWTs. |
| `MCP_OIDC_AUDIENCE` | No | _(empty)_ | Expected `aud` claim for MCP-issued JWTs. |
| `MCP_OIDC_EMAIL_CLAIM` | No | `email` | Claim name read from the MCP JWT and forwarded as `X-Auth-Request-Email` on the internal MCP→REST call. |
| `MCP_OIDC_GROUPS_CLAIM` | No | `groups` | Claim name read from the MCP JWT and forwarded as `X-Auth-Request-Groups` on the internal MCP→REST call. |

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

`aggregator_proxy.env` in the repo root can hold bare `KEY=VALUE` lines (e.g. `PROVIDER_URL=…`, `PROXY_AUTH_ENABLED=true`). Environment variables take precedence. The file is read automatically on startup if present in the working directory.

### Code style

- Line length: 120 characters
- Docstrings: Google convention
- All functions must be fully type-annotated (mypy strict mode)
- Linter: ruff with rules A, B, C4, D, E, F, G, I, ISC, S, T20, W enabled
