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


"""End-to-end MCP integration tests against the FastAPI app with a mocked aggregator."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from aggregator_proxy.main import app
from aggregator_proxy.mcp_server import build_mcp
from aggregator_proxy.nsi_soap import parse_correlation_id
from aggregator_proxy.reservation_store import ReservationStore
from tests.conftest import (
    build_empty_query_summary_sync_response,
    build_query_notification_sync_response,
    build_query_summary_sync_response,
    make_reservation,
)

CONNECTION_ID = "conn-int-001"


def _mock_aggregator(connection_id: str) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        cid = parse_correlation_id(request.content)
        body = request.content.decode()
        if "queryNotificationSync" in body:
            return httpx.Response(200, content=build_query_notification_sync_response(cid))
        # If the SOAP request asks for a specific connectionId that isn't ours, return empty.
        if f"<connectionId>{connection_id}</connectionId>" not in body and "<connectionId>" in body:
            return httpx.Response(200, content=build_empty_query_summary_sync_response(cid))
        return httpx.Response(
            200,
            content=build_query_summary_sync_response(connection_id=connection_id, correlation_id=cid),
        )

    return handler


@pytest.fixture()
def _app_with_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "proxy_auth_enabled", False)
    monkeypatch.setattr(settings, "mcp_auth_enabled", False)

    store = ReservationStore()
    store.create(make_reservation(connection_id=CONNECTION_ID, description="integration test"))
    app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_aggregator(CONNECTION_ID)))
    app.state.callback_client = httpx.AsyncClient()
    app.state.reservation_store = store


async def test_list_reservations_via_mcp(_app_with_reservation: None) -> None:
    mcp = build_mcp(app)
    async with Client(mcp) as client:
        result = await client.call_tool("list_reservations")

    payload = result.data
    ids = [r.connectionId for r in payload.reservations]
    assert CONNECTION_ID in ids


async def test_get_reservation_via_mcp(_app_with_reservation: None) -> None:
    mcp = build_mcp(app)
    async with Client(mcp) as client:
        result = await client.call_tool("get_reservation", {"connectionId": CONNECTION_ID})

    payload = result.data
    assert payload.connectionId == CONNECTION_ID
    assert payload.description == "integration test"


async def test_get_reservation_unknown_id_errors(_app_with_reservation: None) -> None:
    """A missing connection ID surfaces as a tool error (not a silent empty result)."""
    mcp = build_mcp(app)
    async with Client(mcp) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("get_reservation", {"connectionId": "does-not-exist"})
    msg = str(excinfo.value)
    assert "404" in msg or "not found" in msg.lower()
