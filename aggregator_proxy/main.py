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
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

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
    try:
        await _refresh_all_reservations(app.state.nsi_client, app.state.reservation_store)
        logger.info("Startup query completed, reservation store populated")
    except Exception:
        logger.error("Failed to query aggregator on startup, starting with empty store")
    yield
    logger.info("Shutting down NSI Aggregator Proxy")
    await app.state.nsi_client.aclose()
    await app.state.callback_client.aclose()


app = FastAPI(
    title="NSI Aggregator Proxy",
    description=("REST proxy exposing a simplified connection state-machine on top of an NSI aggregator."),
    version=APP_VERSION,
    lifespan=lifespan,
)

app.include_router(reservations.router)
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
