"""FastAPI dependencies shared across routers."""

import httpx
from fastapi import Request

from aggregator_proxy.reservation_store import ReservationStore


def get_nsi_client(request: Request) -> httpx.AsyncClient:
    """Return the shared NSI httpx client stored in application state."""
    client: httpx.AsyncClient = request.app.state.nsi_client
    return client


def get_callback_client(request: Request) -> httpx.AsyncClient:
    """Return the shared callback httpx client stored in application state."""
    client: httpx.AsyncClient = request.app.state.callback_client
    return client


def get_reservation_store(request: Request) -> ReservationStore:
    """Return the shared ReservationStore stored in application state."""
    store: ReservationStore = request.app.state.reservation_store
    return store
