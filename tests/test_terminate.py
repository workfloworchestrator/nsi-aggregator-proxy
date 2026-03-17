"""Tests for DELETE /reservations/{connectionId} (terminate)."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from aggregator_proxy.main import app
from aggregator_proxy.models import P2PS, CriteriaResponse, ReservationStatus
from aggregator_proxy.nsi_soap import parse_correlation_id
from aggregator_proxy.nsi_soap.namespaces import NSMAP
from aggregator_proxy.reservation_store import Reservation, ReservationStore

_C = NSMAP["nsi_ctypes"]
_H = NSMAP["nsi_headers"]
_S = NSMAP["soapenv"]

CALLBACK_URL = "http://callback.example.com/result"
CONNECTION_ID = "test-conn-789"


def _make_soap(body_xml: str, correlation_id: str) -> bytes:
    """Wrap an NSI body element in a full SOAP envelope with nsiHeader."""
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="{_S}" xmlns:head="{_H}" xmlns:type="{_C}">
  <soapenv:Header>
    <head:nsiHeader>
      <correlationId>{correlation_id}</correlationId>
    </head:nsiHeader>
  </soapenv:Header>
  <soapenv:Body>
    {body_xml}
  </soapenv:Body>
</soapenv:Envelope>""".encode()


def _acknowledgment_xml(correlation_id: str) -> bytes:
    return _make_soap("<acknowledgment/>", correlation_id)


def _terminate_confirmed_xml(correlation_id: str) -> bytes:
    return _make_soap(
        f"<terminateConfirmed><connectionId>{CONNECTION_ID}</connectionId></terminateConfirmed>",
        correlation_id=correlation_id,
    )


def _make_reservation(status: ReservationStatus = ReservationStatus.RESERVED) -> Reservation:
    return Reservation(
        connection_id=CONNECTION_ID,
        status=status,
        global_reservation_id=None,
        description="test reservation",
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
        callback_url=CALLBACK_URL,
    )


def _get_pending_correlation_id(store: ReservationStore) -> str:
    """Extract the single pending correlation_id from the store."""
    keys = list(store._pending.keys())  # noqa: SLF001
    assert len(keys) == 1, f"Expected exactly 1 pending, got {len(keys)}"
    return keys[0]


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


def _nsi_handler(request: httpx.Request) -> httpx.Response:
    cid = parse_correlation_id(request.content)
    return httpx.Response(200, content=_acknowledgment_xml(cid))


def _callback_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200)


class TestTerminateValidation:
    """Test request validation (404, 409)."""

    def test_terminate_unknown_connection_returns_404(self, client: TestClient) -> None:
        resp = client.request(
            "DELETE",
            "/reservations/nonexistent",
            json={"callbackURL": CALLBACK_URL},
        )
        assert resp.status_code == 404

    def test_terminate_activated_state_returns_409(self, client: TestClient, store: ReservationStore) -> None:
        store.create(_make_reservation(status=ReservationStatus.ACTIVATED))
        resp = client.request(
            "DELETE",
            f"/reservations/{CONNECTION_ID}",
            json={"callbackURL": CALLBACK_URL},
        )
        assert resp.status_code == 409


class TestTerminateFromReserved:
    """Test successful terminate from RESERVED → TERMINATED."""

    @pytest.mark.anyio()
    async def test_terminate_from_reserved(self, store: ReservationStore) -> None:
        store.create(_make_reservation(status=ReservationStatus.RESERVED))

        async with httpx.AsyncClient(transport=httpx.MockTransport(_nsi_handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(_callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:
                    resp = await test_client.request(
                        "DELETE",
                        f"/reservations/{CONNECTION_ID}",
                        json={"callbackURL": CALLBACK_URL},
                    )
                    assert resp.status_code == 202

                    await asyncio.sleep(0.05)

                    cid = _get_pending_correlation_id(store)

                    # Simulate terminateConfirmed callback
                    await test_client.post("/nsi/v2/callback", content=_terminate_confirmed_xml(cid))
                    await asyncio.sleep(0.1)

                    assert store.get(CONNECTION_ID).status == ReservationStatus.TERMINATED  # type: ignore[union-attr]


class TestTerminateFromFailed:
    """Test successful terminate from FAILED → TERMINATED."""

    @pytest.mark.anyio()
    async def test_terminate_from_failed(self, store: ReservationStore) -> None:
        store.create(_make_reservation(status=ReservationStatus.FAILED))

        async with httpx.AsyncClient(transport=httpx.MockTransport(_nsi_handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(_callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:
                    resp = await test_client.request(
                        "DELETE",
                        f"/reservations/{CONNECTION_ID}",
                        json={"callbackURL": CALLBACK_URL},
                    )
                    assert resp.status_code == 202

                    await asyncio.sleep(0.05)

                    cid = _get_pending_correlation_id(store)

                    # Simulate terminateConfirmed callback
                    await test_client.post("/nsi/v2/callback", content=_terminate_confirmed_xml(cid))
                    await asyncio.sleep(0.1)

                    assert store.get(CONNECTION_ID).status == ReservationStatus.TERMINATED  # type: ignore[union-attr]


class TestTerminateTimeout:
    """Test timeout → TERMINATED (not FAILED, since both paths end in TERMINATED)."""

    @pytest.mark.anyio()
    async def test_terminate_timeout(self, store: ReservationStore, monkeypatch: pytest.MonkeyPatch) -> None:
        from aggregator_proxy import settings as settings_module

        monkeypatch.setattr(settings_module.settings, "nsi_timeout", 0.1)

        store.create(_make_reservation(status=ReservationStatus.RESERVED))

        async with httpx.AsyncClient(transport=httpx.MockTransport(_nsi_handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(_callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:
                    resp = await test_client.request(
                        "DELETE",
                        f"/reservations/{CONNECTION_ID}",
                        json={"callbackURL": CALLBACK_URL},
                    )
                    assert resp.status_code == 202

                    await asyncio.sleep(0.3)

                    assert store.get(CONNECTION_ID).status == ReservationStatus.TERMINATED  # type: ignore[union-attr]
