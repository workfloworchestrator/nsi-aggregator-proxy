"""Tests for GET /reservations and GET /reservations/{connectionId}."""

import httpx
import pytest
from fastapi.testclient import TestClient

from aggregator_proxy.main import app
from aggregator_proxy.models import P2PS, CriteriaResponse, ReservationStatus
from aggregator_proxy.nsi_soap import parse_correlation_id
from aggregator_proxy.reservation_store import Reservation, ReservationStore
from tests.conftest import (
    build_empty_query_summary_sync_response,
    build_query_notification_sync_response,
    build_query_summary_sync_response,
)

CONNECTION_ID_1 = "conn-001"
CONNECTION_ID_2 = "conn-002"


def _make_reservation(
    connection_id: str = CONNECTION_ID_1,
    status: ReservationStatus = ReservationStatus.RESERVED,
    global_reservation_id: str | None = "urn:uuid:550e8400-e29b-41d4-a716-446655440000",
    description: str = "test reservation",
) -> Reservation:
    return Reservation(
        connection_id=connection_id,
        status=status,
        global_reservation_id=global_reservation_id,
        description=description,
        criteria=CriteriaResponse(
            version=1,
            serviceType="http://services.ogf.org/nsi/2013/12/descriptions/EVTS.A-GOLE",
            p2ps=P2PS(
                capacity=1000,
                sourceSTP="urn:ogf:network:example.net:2025:src?vlan=100",
                destSTP="urn:ogf:network:example.net:2025:dst?vlan=200",
            ),
        ),
        requester_nsa="urn:ogf:network:example.net:2025:nsa:requester",
        provider_nsa="urn:ogf:network:example.net:2025:nsa:provider",
        callback_url="http://callback.example.com/result",
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


def _nsi_handler_with_reservation(connection_id: str, provision_state: str = "Released") -> object:
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
def store() -> ReservationStore:
    return ReservationStore()


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
