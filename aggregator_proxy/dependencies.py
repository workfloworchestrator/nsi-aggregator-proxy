"""FastAPI dependencies shared across routers."""

import httpx
from fastapi import Request


def get_nsi_client(request: Request) -> httpx.AsyncClient:
    """Return the shared NSI httpx client stored in application state."""
    client: httpx.AsyncClient = request.app.state.nsi_client
    return client
