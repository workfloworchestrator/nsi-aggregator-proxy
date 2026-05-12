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
from fastapi.responses import JSONResponse, Response

from aggregator_proxy.auth import OIDCProvider, get_authenticated_user
from aggregator_proxy.logging_config import configure_logging
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
    if settings.auth_enabled and settings.oidc_issuer:
        app.state.oidc_http_client = httpx.AsyncClient()
        jwks_uri = settings.oidc_jwks_uri
        userinfo_uri = settings.oidc_userinfo_uri

        if not jwks_uri or not userinfo_uri:
            oidc_config_url = f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration"
            logger.info("Discovering OIDC configuration", url=oidc_config_url)
            resp = await app.state.oidc_http_client.get(oidc_config_url)
            resp.raise_for_status()
            oidc_config = resp.json()
            jwks_uri = jwks_uri or oidc_config.get("jwks_uri", "")
            userinfo_uri = userinfo_uri or oidc_config.get("userinfo_endpoint", "")

        if not jwks_uri or not userinfo_uri:
            logger.error(
                "OIDC configuration incomplete",
                jwks_uri=bool(jwks_uri),
                userinfo_uri=bool(userinfo_uri),
            )
            raise SystemExit("OIDC requires both jwks_uri and userinfo_endpoint")

        app.state.oidc_provider = OIDCProvider(
            jwks_uri=jwks_uri,
            userinfo_uri=userinfo_uri,
            http_client=app.state.oidc_http_client,
            cache_lifespan=settings.oidc_jwks_cache_lifespan,
            userinfo_cache_ttl=settings.oidc_userinfo_cache_ttl,
        )
        logger.info(
            "OIDC authentication enabled",
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            jwks_uri=jwks_uri,
            userinfo_uri=userinfo_uri,
        )
    else:
        app.state.oidc_provider = None
        app.state.oidc_http_client = None

    if settings.auth_enabled:
        methods = []
        if settings.oidc_issuer:
            methods.append("OIDC")
        if settings.mtls_header:
            methods.append(f"mTLS (header: {settings.mtls_header})")
        logger.info("Authentication enabled", methods=methods)
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
    if app.state.oidc_http_client:
        await app.state.oidc_http_client.aclose()


app = FastAPI(
    title="NSI Aggregator Proxy",
    description=("REST proxy exposing a simplified connection state-machine on top of an NSI aggregator."),
    version=APP_VERSION,
    lifespan=lifespan,
    root_path=settings.root_path,
)

_auth_deps = [Depends(get_authenticated_user)]
app.include_router(reservations.router, dependencies=_auth_deps)
app.include_router(nsi_callback_router)


@app.exception_handler(httpx.HTTPStatusError)
async def aggregator_error_handler(request: Request, exc: httpx.HTTPStatusError) -> JSONResponse:
    """Return 502 when the aggregator returns an error, without logging a stacktrace."""
    logger.error("Unhandled aggregator HTTP error", status_code=exc.response.status_code, url=str(exc.request.url))
    return JSONResponse(status_code=502, content={"detail": "NSI aggregator returned an error"})


@app.get("/health", status_code=200, include_in_schema=False)
async def health() -> Response:
    """Liveness probe endpoint."""
    return Response(status_code=200)


def run() -> None:
    """Entry point invoked by the ``aggregator-proxy`` CLI command."""
    uvicorn.run(
        "aggregator_proxy.main:app",
        host=settings.host,
        port=settings.port,
        log_config=None,
    )
