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

"""FastAPI dependencies shared across routers."""

from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Request, status

from aggregator_proxy.reservation_store import ReservationStore


def get_nsi_client(request: Request) -> httpx.AsyncClient:
    """Return the shared NSI httpx client stored in application state."""
    client: httpx.AsyncClient = request.app.state.nsi_client
    return client


def get_upa_client(request: Request) -> httpx.AsyncClient:
    """Return the shared uPA httpx client, answering 503 while uPA queries are not configured."""
    client: httpx.AsyncClient | None = request.app.state.upa_client
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Querying uPAs needs DDS_PROXY_URL, UPA_CLIENT_CERT and UPA_CLIENT_KEY",
        )
    return client


def get_callback_client(request: Request) -> httpx.AsyncClient:
    """Return the shared callback httpx client stored in application state."""
    client: httpx.AsyncClient = request.app.state.callback_client
    return client


def get_reservation_store(request: Request) -> ReservationStore:
    """Return the shared ReservationStore stored in application state."""
    store: ReservationStore = request.app.state.reservation_store
    return store


NsiClient = Annotated[httpx.AsyncClient, Depends(get_nsi_client)]
UpaClient = Annotated[httpx.AsyncClient, Depends(get_upa_client)]
