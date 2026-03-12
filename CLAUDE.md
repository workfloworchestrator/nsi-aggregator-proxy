# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

This project uses [uv](https://docs.astral.sh/uv/) for dependency and environment management.

```bash
# Install dependencies
uv sync

# Run the application (requires AGGREGATOR_PROXY_AGGREGATOR_URL to be set)
AGGREGATOR_PROXY_AGGREGATOR_URL=https://aggregator.example.com uv run aggregator-proxy

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
- **Settings**: all configuration is via environment variables with the `AGGREGATOR_PROXY_` prefix, managed by `pydantic-settings` (`settings.py`). The only required variable is `AGGREGATOR_PROXY_AGGREGATOR_URL`.

### Module layout

```
aggregator_proxy/
  main.py            # FastAPI app, lifespan, entry point (run())
  settings.py        # pydantic-settings config (env prefix: AGGREGATOR_PROXY_)
  models.py          # Pydantic request/response models and ReservationStatus enum
  nsi_client.py      # httpx.AsyncClient factory with mTLS config
  dependencies.py    # FastAPI dependency: get_nsi_client
  logging_config.py  # structlog + stdlib unified logging pipeline
  routers/
    reservations.py  # All /reservations endpoints (POST, GET, DELETE)
  nsi_soap/
    namespaces.py    # Shared NSMAP dict for all NSI CS v2 XML namespaces
    builder.py       # NsiHeader dataclass + build_reserve / build_reserve_commit /
                     #   build_provision / build_release / build_terminate (lxml)
    parser.py        # parse() dispatcher → typed dataclasses for every inbound
                     #   message type; see module docstring for sync vs async classification
```

### NSI SOAP layer

The `nsi_soap` package handles the translation between the REST layer and the NSI CS v2 SOAP protocol spoken by the aggregator (e.g. Safnari).

**Building requests** — pass an `NsiHeader` (requester/provider NSA URNs, replyTo URL, auto-generated correlationId) plus operation-specific arguments to the relevant `build_*` function; each returns UTF-8 XML bytes ready to POST.

**Parsing responses** — call `parse(xml_bytes)` on any received SOAP envelope. It returns one of the typed dataclasses below; use a `match` statement in the caller to handle each case.

| Dataclass | Sync/Async | Trigger |
|---|---|---|
| `ReserveResponse` | Sync | HTTP response to `reserve` |
| `Acknowledgment` | Sync | HTTP response to `provision`, `release`, `terminate` |
| `ReserveConfirmed` | Async callback | Reserve held, proxy must send `reserveCommit` |
| `ReserveCommitConfirmed` | Async callback | State → RESERVED |
| `ProvisionConfirmed` | Async callback | Awaiting `dataPlaneStateChange` |
| `DataPlaneStateChange` | Async callback | `active=True` → ACTIVATED, `active=False` → RESERVED |
| `ReleaseConfirmed` | Async callback | State → RESERVED |
| `TerminateConfirmed` | Async callback | State → TERMINATED |

### Current implementation status

All router endpoints are stubbed with `# TODO` comments — the NSI aggregator calls, state persistence, and callback delivery are not yet implemented. The skeleton accepts requests and returns 202 responses but does not interact with an actual NSI aggregator.

### Configuration reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `AGGREGATOR_PROXY_AGGREGATOR_URL` | Yes | — | Base URL of the NSI aggregator |
| `AGGREGATOR_PROXY_CLIENT_CERT` | No | None | Path to client TLS certificate |
| `AGGREGATOR_PROXY_CLIENT_KEY` | No | None | Path to client TLS private key |
| `AGGREGATOR_PROXY_CA_FILE` | No | None | Path to CA bundle for server verification |
| `AGGREGATOR_PROXY_LOG_LEVEL` | No | `INFO` | Log level |
| `AGGREGATOR_PROXY_HOST` | No | `0.0.0.0` | Bind host |
| `AGGREGATOR_PROXY_PORT` | No | `8080` | Bind port |

### Code style

- Line length: 120 characters
- Docstrings: Google convention
- All functions must be fully type-annotated (mypy strict mode)
- Linter: ruff with rules A, B, C4, D, E, F, G, I, ISC, S, T20, W enabled
