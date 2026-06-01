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


"""FastAPI application and entry point."""

import importlib.metadata
import platform
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import structlog
import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastmcp.utilities.lifespan import combine_lifespans

from aggregator_proxy.auth import get_authenticated_user, get_mtls_authenticated_callback
from aggregator_proxy.logging_config import configure_logging
from aggregator_proxy.mcp_server import build_mcp
from aggregator_proxy.nsi_client import create_nsi_client
from aggregator_proxy.reservation_store import ReservationStore
from aggregator_proxy.routers import reservations
from aggregator_proxy.routers.nsi_callback import router as nsi_callback_router
from aggregator_proxy.routers.reservations import _refresh_all_reservations
from aggregator_proxy.settings import settings

logger = structlog.get_logger(__name__)

APP_VERSION = importlib.metadata.version("aggregator-proxy")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Configure logging and shared resources on startup."""
    configure_logging()
    logger.info(
        "Starting NSI Aggregator Proxy %s using Python %s (%s) on %s",
        APP_VERSION,
        platform.python_version(),
        platform.python_implementation(),
        platform.node(),
        **settings.model_dump(mode="json"),
    )
    app.state.nsi_client = create_nsi_client()
    app.state.callback_client = httpx.AsyncClient()
    app.state.reservation_store = ReservationStore()

    if settings.proxy_auth_enabled:
        logger.info(
            "Authentication enabled",
            mtls_header=settings.mtls_header or None,
            required_groups=settings.oidc_required_groups,
        )
    else:
        logger.info("Authentication disabled")

    try:
        await _refresh_all_reservations(app.state.nsi_client, app.state.reservation_store)
        logger.info("Startup query completed, reservation store populated")
    except Exception:
        logger.error("Failed to query aggregator on startup, starting with empty store")
    yield
    logger.info("Shutting down NSI Aggregator Proxy")
    await app.state.nsi_client.aclose()
    await app.state.callback_client.aclose()


def create_app() -> FastAPI:
    """Build the FastAPI app.

    ``/openapi.json``, ``/docs``, and ``/redoc`` are served by explicit routes
    that share the same authentication dependency as the data endpoints, so
    they're available to authorised users and rejected with 401/403 otherwise.
    FastAPI's built-in docs routes are disabled because they cannot be put
    behind a ``Depends``. When ``MCP_ENABLED`` is true, the MCP sub-app is
    mounted at ``settings.mcp_path`` and its lifespan combined with this app's.
    """
    auth_deps = [Depends(get_authenticated_user)]
    callback_auth_deps = [Depends(get_mtls_authenticated_callback)]
    fastapi_app = FastAPI(
        title="NSI Aggregator Proxy",
        description=("REST proxy exposing a simplified connection state-machine on top of an NSI aggregator."),
        version=APP_VERSION,
        lifespan=lifespan,
        root_path=settings.root_path,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )

    @fastapi_app.get("/openapi.json", include_in_schema=False, dependencies=auth_deps)
    async def openapi_endpoint(request: Request) -> JSONResponse:
        schema = fastapi_app.openapi()
        root_path = request.scope.get("root_path", "").rstrip("/")
        if root_path and "servers" not in schema:
            schema["servers"] = [{"url": root_path}]
        return JSONResponse(schema)

    @fastapi_app.get("/docs", include_in_schema=False, dependencies=auth_deps)
    async def swagger_ui(request: Request) -> HTMLResponse:
        root_path = request.scope.get("root_path", "").rstrip("/")
        return get_swagger_ui_html(
            openapi_url=root_path + "/openapi.json",
            title=fastapi_app.title + " - Swagger UI",
        )

    @fastapi_app.get("/redoc", include_in_schema=False, dependencies=auth_deps)
    async def redoc(request: Request) -> HTMLResponse:
        root_path = request.scope.get("root_path", "").rstrip("/")
        return get_redoc_html(
            openapi_url=root_path + "/openapi.json",
            title=fastapi_app.title + " - ReDoc",
        )

    fastapi_app.include_router(reservations.router, dependencies=auth_deps)
    fastapi_app.include_router(nsi_callback_router, dependencies=callback_auth_deps)

    @fastapi_app.exception_handler(httpx.HTTPStatusError)
    async def aggregator_error_handler(request: Request, exc: httpx.HTTPStatusError) -> JSONResponse:
        """Return 502 when the aggregator returns an error, without logging a stacktrace."""
        logger.error("Unhandled aggregator HTTP error", status_code=exc.response.status_code, url=str(exc.request.url))
        return JSONResponse(status_code=502, content={"detail": "NSI aggregator returned an error"})

    @fastapi_app.get("/health", status_code=200, include_in_schema=False)
    async def health() -> Response:
        """Liveness probe endpoint."""
        return Response(status_code=200)

    if settings.mcp_enabled:
        _setup_mcp(fastapi_app)

    return fastapi_app


def _setup_mcp(app: FastAPI) -> None:
    """Build the MCP sub-app and mount it on ``app``.

    The MCP-auth / REST-auth invariant is enforced by the Settings model
    validator (``_require_mcp_auth_when_proxy_auth_enabled``), so by the time
    we reach this function the combination is known-valid.
    """
    mcp_app = build_mcp(app).http_app(path="/")
    original_lifespan = app.router.lifespan_context
    app.router.lifespan_context = combine_lifespans(original_lifespan, mcp_app.lifespan)
    app.mount(settings.mcp_path, mcp_app)


app = create_app()


def run() -> None:
    """Entry point invoked by the ``aggregator-proxy`` CLI command."""
    uvicorn.run(
        "aggregator_proxy.main:app",
        host=settings.host,
        port=settings.port,
        log_config=None,
    )
