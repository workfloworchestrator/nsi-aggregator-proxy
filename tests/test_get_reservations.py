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


"""Tests for GET /reservations and GET /reservations/{connectionId}."""

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from aggregator_proxy.main import app
from aggregator_proxy.models import ReservationStatus
from aggregator_proxy.nsi_soap import parse_correlation_id
from aggregator_proxy.reservation_store import Reservation, ReservationStore
from tests.conftest import (
    build_child_xml,
    build_empty_query_summary_sync_response,
    build_query_notification_sync_response,
    build_query_summary_sync_response,
    build_query_summary_sync_response_with_children,
    make_reservation,
)

CONNECTION_ID_1 = "conn-001"
CONNECTION_ID_2 = "conn-002"


def _make_reservation(
    connection_id: str = CONNECTION_ID_1,
    status: ReservationStatus = ReservationStatus.RESERVED,
    global_reservation_id: str | None = "urn:uuid:550e8400-e29b-41d4-a716-446655440000",
    description: str = "test reservation",
) -> Reservation:
    return make_reservation(
        connection_id=connection_id,
        status=status,
        global_reservation_id=global_reservation_id,
        description=description,
    )


def _nsi_handler(request: httpx.Request) -> httpx.Response:
    """Mock NSI handler that responds to querySummarySync with an empty result."""
    cid = parse_correlation_id(request.content)
    body = request.content.decode()
    if "queryNotificationSync" in body:
        return httpx.Response(200, content=build_query_notification_sync_response(cid))
    if "querySummarySync" in body:
        return httpx.Response(200, content=build_empty_query_summary_sync_response(cid))
    return httpx.Response(200)


def _nsi_handler_with_reservation(
    connection_id: str, provision_state: str = "Released"
) -> Callable[[httpx.Request], httpx.Response]:
    """Return an NSI handler that returns a querySummarySyncConfirmed with one reservation."""

    def handler(request: httpx.Request) -> httpx.Response:
        cid = parse_correlation_id(request.content)
        body = request.content.decode()
        if "queryNotificationSync" in body:
            return httpx.Response(200, content=build_query_notification_sync_response(cid))
        return httpx.Response(
            200,
            content=build_query_summary_sync_response(
                connection_id=connection_id,
                correlation_id=cid,
                provision_state=provision_state,
            ),
        )

    return handler


@pytest.fixture()
def _app_state(store: ReservationStore) -> None:
    """Inject test state into the FastAPI app."""
    app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(_nsi_handler))
    app.state.callback_client = httpx.AsyncClient()
    app.state.reservation_store = store


@pytest.fixture()
def client(_app_state: None) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


class TestGetReservation:
    """Tests for GET /reservations/{connectionId}."""

    def test_unknown_connection_returns_404(self, client: TestClient) -> None:
        resp = client.get("/reservations/nonexistent")
        assert resp.status_code == 404

    def test_known_connection_returns_detail(self, client: TestClient, store: ReservationStore) -> None:
        reservation = _make_reservation()
        store.create(reservation)

        # Replace NSI client with one that returns the reservation from the aggregator
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_nsi_handler_with_reservation(CONNECTION_ID_1))
        )

        resp = client.get(f"/reservations/{CONNECTION_ID_1}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["connectionId"] == CONNECTION_ID_1
        assert body["description"] == "test reservation"
        assert body["status"] == "RESERVED"
        assert body["criteria"]["version"] == 1
        assert body["criteria"]["p2ps"]["capacity"] == 1000
        assert body["criteria"]["p2ps"]["sourceSTP"] == "urn:ogf:network:example.net:2025:src?vlan=100"
        assert body["criteria"]["p2ps"]["destSTP"] == "urn:ogf:network:example.net:2025:dst?vlan=200"

    def test_known_connection_without_global_id(self, client: TestClient, store: ReservationStore) -> None:
        store.create(_make_reservation(global_reservation_id=None))

        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_nsi_handler_with_reservation(CONNECTION_ID_1))
        )

        resp = client.get(f"/reservations/{CONNECTION_ID_1}")

        assert resp.status_code == 200
        assert resp.json()["globalReservationId"] is None


class TestGetReservationAggregatorFailure:
    """Test when the aggregator is unreachable during GET /reservations/{connectionId}."""

    def test_aggregator_unreachable_returns_502(self, store: ReservationStore) -> None:
        store.create(_make_reservation())

        def failing_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(failing_handler))
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get(f"/reservations/{CONNECTION_ID_1}")
        assert resp.status_code == 502


class TestListReservations:
    """Tests for GET /reservations."""

    def test_empty_store_returns_empty_list(self, client: TestClient) -> None:
        resp = client.get("/reservations")
        assert resp.status_code == 200
        assert resp.json() == {"reservations": []}

    def test_multiple_reservations_returned(self, client: TestClient, store: ReservationStore) -> None:
        store.create(_make_reservation(connection_id=CONNECTION_ID_1, status=ReservationStatus.RESERVED))
        store.create(
            _make_reservation(
                connection_id=CONNECTION_ID_2,
                status=ReservationStatus.ACTIVATED,
                description="second reservation",
            )
        )

        resp = client.get("/reservations")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["reservations"]) == 2
        ids = {r["connectionId"] for r in body["reservations"]}
        assert ids == {CONNECTION_ID_1, CONNECTION_ID_2}


class TestListReservationsAggregatorFailure:
    """Test when the aggregator is unreachable during GET /reservations."""

    def test_aggregator_unreachable_returns_502(self, store: ReservationStore) -> None:
        def failing_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(failing_handler))
        app.state.callback_client = httpx.AsyncClient()
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/reservations")
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# Detail query parameter tests
# ---------------------------------------------------------------------------

_CHILDREN_XML = build_child_xml(
    order=0,
    connection_id="child-seg-0",
    provider_nsa="urn:ogf:network:west.example.net:2025:nsa:supa",
    source_stp="urn:ogf:network:west.example.net:2025:port-a?vlan=100",
    dest_stp="urn:ogf:network:west.example.net:2025:port-b?vlan=200",
    capacity=1000,
) + build_child_xml(
    order=1,
    connection_id="child-seg-1",
    provider_nsa="urn:ogf:network:east.example.net:2025:nsa:supa",
    source_stp="urn:ogf:network:east.example.net:2025:port-c?vlan=200",
    dest_stp="urn:ogf:network:east.example.net:2025:port-d?vlan=300",
    capacity=1000,
)


def _nsi_handler_with_children(connection_id: str) -> Callable[[httpx.Request], httpx.Response]:
    """Return an NSI handler that returns querySummarySyncConfirmed with children."""

    def handler(request: httpx.Request) -> httpx.Response:
        cid = parse_correlation_id(request.content)
        body = request.content.decode()
        if "queryNotificationSync" in body:
            return httpx.Response(200, content=build_query_notification_sync_response(cid))
        return httpx.Response(
            200,
            content=build_query_summary_sync_response_with_children(
                connection_id=connection_id,
                correlation_id=cid,
                children_xml=_CHILDREN_XML,
            ),
        )

    return handler


class TestGetReservationDetailParameter:
    """Tests for the detail query parameter on GET /reservations/{connectionId}."""

    def test_default_detail_has_no_segments(self, client: TestClient, store: ReservationStore) -> None:
        store.create(_make_reservation())
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_nsi_handler_with_reservation(CONNECTION_ID_1))
        )

        resp = client.get(f"/reservations/{CONNECTION_ID_1}")
        assert resp.status_code == 200
        assert resp.json()["segments"] is None

    def test_detail_summary_has_no_segments(self, client: TestClient, store: ReservationStore) -> None:
        store.create(_make_reservation())
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_nsi_handler_with_reservation(CONNECTION_ID_1))
        )

        resp = client.get(f"/reservations/{CONNECTION_ID_1}?detail=summary")
        assert resp.status_code == 200
        assert resp.json()["segments"] is None

    def test_detail_full_returns_segments(self, client: TestClient, store: ReservationStore) -> None:
        store.create(_make_reservation())
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_nsi_handler_with_children(CONNECTION_ID_1))
        )

        resp = client.get(f"/reservations/{CONNECTION_ID_1}?detail=full")
        assert resp.status_code == 200
        body = resp.json()
        segments = body["segments"]
        assert segments is not None
        assert len(segments) == 2

        assert segments[0]["order"] == 0
        assert segments[0]["connectionId"] == "child-seg-0"
        assert segments[0]["providerNSA"] == "urn:ogf:network:west.example.net:2025:nsa:supa"
        assert segments[0]["capacity"] == 1000
        assert segments[0]["sourceSTP"] == "urn:ogf:network:west.example.net:2025:port-a?vlan=100"
        assert segments[0]["destSTP"] == "urn:ogf:network:west.example.net:2025:port-b?vlan=200"
        assert segments[0]["status"] is None

        assert segments[1]["order"] == 1
        assert segments[1]["connectionId"] == "child-seg-1"

    def test_detail_full_no_children_returns_no_segments(self, client: TestClient, store: ReservationStore) -> None:
        store.create(_make_reservation())
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_nsi_handler_with_reservation(CONNECTION_ID_1))
        )

        resp = client.get(f"/reservations/{CONNECTION_ID_1}?detail=full")
        assert resp.status_code == 200
        assert resp.json()["segments"] is None

    def test_invalid_detail_returns_422(self, client: TestClient, store: ReservationStore) -> None:
        store.create(_make_reservation())
        resp = client.get(f"/reservations/{CONNECTION_ID_1}?detail=invalid")
        assert resp.status_code == 422


class TestListReservationsDetailParameter:
    """Tests for the detail query parameter on GET /reservations."""

    def test_detail_recursive_returns_400(self, client: TestClient) -> None:
        resp = client.get("/reservations?detail=recursive")
        assert resp.status_code == 400
        assert "recursive" in resp.json()["detail"].lower()

    def test_detail_full_returns_segments(self, client: TestClient, store: ReservationStore) -> None:
        store.create(_make_reservation())

        def handler(request: httpx.Request) -> httpx.Response:
            cid = parse_correlation_id(request.content)
            body = request.content.decode()
            if "queryNotificationSync" in body:
                return httpx.Response(200, content=build_query_notification_sync_response(cid))
            return httpx.Response(
                200,
                content=build_query_summary_sync_response_with_children(
                    connection_id=CONNECTION_ID_1,
                    correlation_id=cid,
                    children_xml=_CHILDREN_XML,
                ),
            )

        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        resp = client.get("/reservations?detail=full")
        assert resp.status_code == 200
        reservations = resp.json()["reservations"]
        assert len(reservations) == 1
        segments = reservations[0]["segments"]
        assert segments is not None
        assert len(segments) == 2
        assert segments[0]["connectionId"] == "child-seg-0"

    def test_default_detail_has_no_segments(self, client: TestClient) -> None:
        resp = client.get("/reservations")
        assert resp.status_code == 200
        for r in resp.json()["reservations"]:
            assert r["segments"] is None
