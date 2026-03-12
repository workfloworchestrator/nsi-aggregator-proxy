"""FastAPI application and entry point."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response

from aggregator_proxy.logging_config import configure_logging
from aggregator_proxy.nsi_client import create_nsi_client
from aggregator_proxy.routers import reservations
from aggregator_proxy.settings import settings

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Configure logging and shared resources on startup."""
    configure_logging()
    logger.info(
        "Starting NSI Aggregator Proxy",
        aggregator_url=settings.aggregator_url,
        host=settings.host,
        port=settings.port,
    )
    app.state.nsi_client = create_nsi_client()
    yield
    logger.info("Shutting down NSI Aggregator Proxy")
    await app.state.nsi_client.aclose()


app = FastAPI(
    title="NSI Aggregator Proxy",
    description=(
        "REST proxy exposing a simplified connection state-machine "
        "on top of an NSI aggregator."
    ),
    version="0.1.1.dev1",
    lifespan=lifespan,
)

app.include_router(reservations.router)


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
