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

### Module layout

```
aggregator_proxy/
  main.py               # FastAPI app, lifespan, entry point (run()), /health endpoint
  settings.py           # pydantic-settings config (env prefix: AGGREGATOR_PROXY_)
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

### Code style

- Line length: 120 characters
- Docstrings: Google convention
- All functions must be fully type-annotated (mypy strict mode)
- Linter: ruff with rules A, B, C4, D, E, F, G, I, ISC, S, T20, W enabled
