"""Tests for GET /reservations and GET /reservations/{connectionId}."""

import httpx
import pytest
from fastapi.testclient import TestClient

from aggregator_proxy.main import app
from aggregator_proxy.models import P2PS, CriteriaResponse, ReservationStatus
from aggregator_proxy.reservation_store import Reservation, ReservationStore

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


@pytest.fixture()
def store() -> ReservationStore:
    return ReservationStore()


@pytest.fixture()
def _app_state(store: ReservationStore) -> None:
    """Inject test state into the FastAPI app."""
    app.state.nsi_client = httpx.AsyncClient()
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

        resp = client.get(f"/reservations/{CONNECTION_ID_1}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["connectionId"] == CONNECTION_ID_1
        assert body["globalReservationId"] == "urn:uuid:550e8400-e29b-41d4-a716-446655440000"
        assert body["description"] == "test reservation"
        assert body["status"] == "RESERVED"
        assert body["criteria"]["version"] == 1
        assert body["criteria"]["p2ps"]["capacity"] == 1000
        assert body["criteria"]["p2ps"]["sourceSTP"] == "urn:ogf:network:example.net:2025:src?vlan=100"
        assert body["criteria"]["p2ps"]["destSTP"] == "urn:ogf:network:example.net:2025:dst?vlan=200"

    def test_known_connection_without_global_id(self, client: TestClient, store: ReservationStore) -> None:
        store.create(_make_reservation(global_reservation_id=None))

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
        statuses = {r["connectionId"]: r["status"] for r in body["reservations"]}
        assert statuses[CONNECTION_ID_1] == "RESERVED"
        assert statuses[CONNECTION_ID_2] == "ACTIVATED"
