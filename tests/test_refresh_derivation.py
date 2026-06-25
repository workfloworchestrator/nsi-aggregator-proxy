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


"""End-to-end derivation: a refresh re-derives a stable status from the aggregator's durable history.

These cover the restart case (empty store, status reconstructed from querySummarySync +
queryNotificationSync + queryResultSync) and the stuck-data-plane verdict the proxy must reach so the
orchestrator's reconcile can repair the subscription.
"""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from aggregator_proxy.main import app
from aggregator_proxy.models import ReservationStatus
from aggregator_proxy.nsi_soap import parse_correlation_id
from aggregator_proxy.reservation_store import ReservationStore
from tests.conftest import (
    build_data_plane_state_change_xml,
    build_query_notification_sync_response,
    build_query_result_sync_response,
    build_query_summary_sync_response,
    build_result_xml,
    make_reservation,
)

CONNECTION_ID = "conn-stuck-001"


def _iso(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _handler(
    provision_state: str,
    data_plane_active: bool,
    results_xml: str = "",
    notifications_xml: str = "",
) -> Callable[[httpx.Request], httpx.Response]:
    """Mock aggregator: summary states, notification history, and result history per query."""

    def handler(request: httpx.Request) -> httpx.Response:
        cid = parse_correlation_id(request.content)
        body = request.content.decode()
        if "queryNotificationSync" in body:
            return httpx.Response(200, content=build_query_notification_sync_response(cid, notifications_xml))
        if "queryResultSync" in body:
            return httpx.Response(200, content=build_query_result_sync_response(cid, results_xml))
        if "querySummarySync" in body:
            return httpx.Response(
                200,
                content=build_query_summary_sync_response(
                    connection_id=CONNECTION_ID,
                    correlation_id=cid,
                    provision_state=provision_state,
                    data_plane_active=data_plane_active,
                ),
            )
        return httpx.Response(200)

    return handler


def _client(store: ReservationStore, handler: Callable[[httpx.Request], httpx.Response]) -> TestClient:
    app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.callback_client = httpx.AsyncClient()
    app.state.reservation_store = store
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("provision_state", "data_plane_active", "results_xml", "notifications_xml", "expected"),
    [
        pytest.param(
            "Provisioned",
            False,
            build_result_xml(3, _iso(3600), "provisionConfirmed", CONNECTION_ID),
            "",
            "FAILED",
            id="stuck-activation-past-timeout",
        ),
        pytest.param(
            "Provisioned",
            False,
            build_result_xml(3, _iso(1), "provisionConfirmed", CONNECTION_ID),
            "",
            "ACTIVATING",
            id="recent-activation-within-grace",
        ),
        pytest.param(
            "Provisioned",
            False,
            build_result_xml(3, _iso(3600), "provisionConfirmed", CONNECTION_ID),
            build_data_plane_state_change_xml(connection_id=CONNECTION_ID, timestamp=_iso(3590), active=True),
            "FAILED",
            id="came-up-then-dropped-unsolicited",
        ),
        pytest.param(
            "Released",
            True,
            build_result_xml(4, _iso(3600), "releaseConfirmed", CONNECTION_ID),
            "",
            "FAILED",
            id="stuck-deactivation-past-timeout",
        ),
        pytest.param("Provisioned", True, "", "", "ACTIVATED", id="active-passthrough"),
    ],
)
def test_get_derives_status(
    store: ReservationStore,
    provision_state: str,
    data_plane_active: bool,
    results_xml: str,
    notifications_xml: str,
    expected: str,
) -> None:
    # Empty store == a fresh restart: the status is reconstructed entirely from the aggregator.
    client = _client(store, _handler(provision_state, data_plane_active, results_xml, notifications_xml))

    resp = client.get(f"/reservations/{CONNECTION_ID}")

    assert resp.status_code == 200
    assert resp.json()["status"] == expected


def test_stuck_activation_reports_reason(store: ReservationStore) -> None:
    client = _client(
        store,
        _handler(
            "Provisioned", False, results_xml=build_result_xml(3, _iso(3600), "provisionConfirmed", CONNECTION_ID)
        ),
    )

    body = client.get(f"/reservations/{CONNECTION_ID}").json()

    assert body["status"] == "FAILED"
    assert body["lastError"] is not None


def test_recovery_clears_last_error(store: ReservationStore) -> None:
    reservation = make_reservation(connection_id=CONNECTION_ID, status=ReservationStatus.FAILED)
    reservation.last_error = "data plane not active within 300s of provision"
    store.create(reservation)
    client = _client(store, _handler("Provisioned", True))

    body = client.get(f"/reservations/{CONNECTION_ID}").json()

    assert body["status"] == "ACTIVATED"
    assert body["lastError"] is None


def test_startup_list_derives_failed(store: ReservationStore) -> None:
    # The list endpoint drives _refresh_all_reservations (the startup path).
    client = _client(
        store,
        _handler(
            "Provisioned", False, results_xml=build_result_xml(3, _iso(3600), "provisionConfirmed", CONNECTION_ID)
        ),
    )

    resp = client.get("/reservations")

    assert resp.status_code == 200
    reservations = resp.json()["reservations"]
    assert len(reservations) == 1
    assert reservations[0]["status"] == "FAILED"
