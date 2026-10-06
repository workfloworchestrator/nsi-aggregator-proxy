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


"""Tests for the lastError a failed provision or release reports when its data plane does not change."""

import asyncio
import json
from dataclasses import dataclass, field

import httpx
import pytest

from aggregator_proxy import settings as settings_module
from aggregator_proxy.main import app
from aggregator_proxy.models import ReservationStatus
from aggregator_proxy.nsi_soap import parse_correlation_id
from aggregator_proxy.reservation_store import ReservationStore
from tests.conftest import (
    build_acknowledgment_xml,
    build_error_event_xml,
    build_query_notification_sync_response,
    build_query_summary_sync_response,
    build_soap_envelope,
    get_pending_correlation_id,
    make_reservation,
)

CALLBACK_URL = "http://callback.example.com/result"
CONNECTION_ID = "test-conn-dp"
SUPA_NSA = "urn:ogf:network:canarie.ca:2025:nsa:supa"
SOCKET_ERROR = "GENERIC_RM_ERROR: Could not open socket to 192.0.2.1:830"


@dataclass(frozen=True)
class Operation:
    """One data-plane operation: the state it starts from and the NSI messages it exchanges."""

    path: str
    start_status: ReservationStatus
    provision_state: str
    data_plane_active: bool
    confirmed: str
    failed_event: str


PROVISION = Operation(
    "provision", ReservationStatus.RESERVED, "Released", False, "provisionConfirmed", "activateFailed"
)
RELEASE = Operation("release", ReservationStatus.ACTIVATED, "Provisioned", True, "releaseConfirmed", "deactivateFailed")
OPERATIONS = [pytest.param(PROVISION, id="provision"), pytest.param(RELEASE, id="release")]


@dataclass
class FakeAggregator:
    """Answers the proxy's NSI requests, with ``error_events`` as the connection's notification history."""

    operation: Operation
    error_events: list[str] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        cid = parse_correlation_id(request.content)
        body = request.content.decode()
        if "queryNotificationSync" in body:
            return httpx.Response(200, content=build_query_notification_sync_response(cid, *self.error_events))
        if "querySummarySync" in body:
            return httpx.Response(
                200,
                content=build_query_summary_sync_response(
                    connection_id=CONNECTION_ID,
                    correlation_id=cid,
                    provision_state=self.operation.provision_state,
                    data_plane_active=self.operation.data_plane_active,
                ),
            )
        return httpx.Response(200, content=build_acknowledgment_xml(cid))


def _error_event(event: str) -> str:
    return build_error_event_xml(
        connection_id=CONNECTION_ID,
        event=event,
        originating_nsa=SUPA_NSA,
        error_id="00800",
        error_text=SOCKET_ERROR,
    )


async def _run(
    store: ReservationStore, operation: Operation, *, error_in_history: bool, error_callback: bool
) -> list[dict[str, object]]:
    """Start and confirm the operation, let it fail as specified, and return the callbacks delivered."""
    store.create(
        make_reservation(connection_id=CONNECTION_ID, status=operation.start_status, callback_url=CALLBACK_URL)
    )
    aggregator = FakeAggregator(operation)
    delivered: list[dict[str, object]] = []

    def callback_handler(request: httpx.Request) -> httpx.Response:
        delivered.append(json.loads(request.content))
        return httpx.Response(200)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(aggregator)) as nsi_client,
        httpx.AsyncClient(transport=httpx.MockTransport(callback_handler)) as cb_client,
    ):
        app.state.nsi_client = nsi_client
        app.state.callback_client = cb_client
        app.state.reservation_store = store
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/reservations/{CONNECTION_ID}/{operation.path}", json={"callbackURL": CALLBACK_URL}
            )
            assert resp.status_code == 202
            # Only now: an error event already in the history would make the request's refresh report FAILED.
            aggregator.error_events = [_error_event(operation.failed_event)] if error_in_history else []
            await asyncio.sleep(0.05)
            confirmed = f"<{operation.confirmed}><connectionId>{CONNECTION_ID}</connectionId></{operation.confirmed}>"
            await client.post(
                "/nsi/v2/callback", content=build_soap_envelope(confirmed, get_pending_correlation_id(store))
            )
            await asyncio.sleep(0.05)
            if error_callback:
                event_xml = _error_event(operation.failed_event)
                await client.post("/nsi/v2/callback", content=build_soap_envelope(event_xml, "urn:uuid:agg"))
            await asyncio.sleep(0.3)
    return delivered


def _expected_error(operation: Operation) -> str:
    return f"{operation.failed_event}: 00800: {SOCKET_ERROR} (nsaId={SUPA_NSA})"


@pytest.mark.anyio()
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_error_event_callback_fails_at_once(
    store: ReservationStore, operation: Operation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An errorEvent ends the wait with the provider's error, long before the data-plane timeout."""
    monkeypatch.setattr(settings_module.settings, "dataplane_timeout", 60)

    delivered = await _run(store, operation, error_in_history=True, error_callback=True)

    assert store.get(CONNECTION_ID).status == ReservationStatus.FAILED  # type: ignore[union-attr]
    assert [d["lastError"] for d in delivered] == [_expected_error(operation)]


@pytest.mark.anyio()
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize(
    "error_in_history", [pytest.param(True, id="error-in-history"), pytest.param(False, id="no-history")]
)
async def test_timeout_reports_the_notification_history(
    store: ReservationStore, operation: Operation, error_in_history: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost errorEvent callback is recovered from queryNotificationSync when the wait times out."""
    monkeypatch.setattr(settings_module.settings, "dataplane_timeout", 0.1)

    delivered = await _run(store, operation, error_in_history=error_in_history, error_callback=False)

    target_active = operation is PROVISION
    fallback = f"no DataPlaneStateChange(active={target_active}) received within timeout"
    assert store.get(CONNECTION_ID).status == ReservationStatus.FAILED  # type: ignore[union-attr]
    assert [d["lastError"] for d in delivered] == [_expected_error(operation) if error_in_history else fallback]
