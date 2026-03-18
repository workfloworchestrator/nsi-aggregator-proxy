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


"""Tests for POST /reservations (reserve endpoint)."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from aggregator_proxy.main import app
from aggregator_proxy.models import ReservationStatus
from aggregator_proxy.nsi_soap import parse_correlation_id
from aggregator_proxy.nsi_soap.namespaces import NSMAP
from aggregator_proxy.reservation_store import ReservationStore
from tests.conftest import (
    build_empty_query_summary_sync_response,
    build_query_notification_sync_response,
)

_C = NSMAP["nsi_ctypes"]
_H = NSMAP["nsi_headers"]
_S = NSMAP["soapenv"]
_P = NSMAP["nsi_p2p"]

CALLBACK_URL = "http://callback.example.com/result"
PROVIDER_NSA = "urn:ogf:network:example.net:2025:nsa:provider"
REQUESTER_NSA = "urn:ogf:network:example.net:2025:nsa:requester"


def _make_soap(body_xml: str, correlation_id: str) -> bytes:
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


def _reserve_response_xml(correlation_id: str, connection_id: str = "agg-conn-001") -> bytes:
    return _make_soap(
        f"<reserveResponse><connectionId>{connection_id}</connectionId></reserveResponse>",
        correlation_id,
    )


def _reserve_confirmed_xml(correlation_id: str, connection_id: str = "agg-conn-001") -> bytes:
    return _make_soap(
        f"""\
<reserveConfirmed>
  <connectionId>{connection_id}</connectionId>
  <criteria version="1">
    <serviceType>http://services.ogf.org/nsi/2013/12/descriptions/EVTS.A-GOLE</serviceType>
    <p2p:p2ps xmlns:p2p="{_P}">
      <capacity>1000</capacity>
      <sourceSTP>urn:ogf:network:example.net:2025:src?vlan=100</sourceSTP>
      <destSTP>urn:ogf:network:example.net:2025:dst?vlan=200</destSTP>
    </p2p:p2ps>
  </criteria>
</reserveConfirmed>""",
        correlation_id,
    )


def _reserve_commit_confirmed_xml(correlation_id: str, connection_id: str = "agg-conn-001") -> bytes:
    return _make_soap(
        f"<reserveCommitConfirmed><connectionId>{connection_id}</connectionId></reserveCommitConfirmed>",
        correlation_id,
    )


def _reserve_failed_xml(correlation_id: str, connection_id: str = "agg-conn-001") -> bytes:
    return _make_soap(
        f"""\
<reserveFailed>
  <connectionId>{connection_id}</connectionId>
  <serviceException>
    <nsaId>urn:ogf:network:child:2025:nsa</nsaId>
    <connectionId>{connection_id}</connectionId>
    <errorId>00700</errorId>
    <text>CAPACITY_UNAVAILABLE</text>
  </serviceException>
</reserveFailed>""",
        correlation_id,
    )


def _reserve_timeout_xml(correlation_id: str, connection_id: str = "agg-conn-001") -> bytes:
    return _make_soap(
        f"""\
<reserveTimeout>
  <connectionId>{connection_id}</connectionId>
  <notificationId>1</notificationId>
  <timeStamp>2025-06-01T12:00:00Z</timeStamp>
  <timeoutValue>180</timeoutValue>
  <originatingConnectionId>orig-conn-1</originatingConnectionId>
  <originatingNSA>urn:ogf:network:child:2025:nsa</originatingNSA>
</reserveTimeout>""",
        correlation_id,
    )


def _acknowledgment_xml(correlation_id: str) -> bytes:
    return _make_soap("<acknowledgment/>", correlation_id)


def _reserve_request_body(
    provider_nsa: str = PROVIDER_NSA,
    callback_url: str = CALLBACK_URL,
    global_reservation_id: str | None = None,
) -> dict:
    body: dict = {
        "description": "test circuit",
        "criteria": {
            "p2ps": {
                "capacity": 1000,
                "sourceSTP": "urn:ogf:network:example.net:2025:src?vlan=100",
                "destSTP": "urn:ogf:network:example.net:2025:dst?vlan=200",
            },
        },
        "requesterNSA": REQUESTER_NSA,
        "providerNSA": provider_nsa,
        "callbackURL": callback_url,
    }
    if global_reservation_id is not None:
        body["globalReservationId"] = global_reservation_id
    return body


def _get_pending_correlation_id(store: ReservationStore) -> str:
    keys = list(store._pending.keys())  # noqa: SLF001
    assert len(keys) == 1, f"Expected exactly 1 pending, got {len(keys)}"
    return keys[0]


@pytest.fixture()
def store() -> ReservationStore:
    return ReservationStore()


class TestReserveValidation:
    """Test request validation."""

    def test_wrong_provider_nsa_returns_400(self, store: ReservationStore) -> None:
        def nsi_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(nsi_handler))
        app.state.callback_client = httpx.AsyncClient()
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/reservations",
            json=_reserve_request_body(provider_nsa="urn:ogf:network:wrong:2025:nsa:wrong"),
        )
        assert resp.status_code == 400
        assert "providerNSA" in resp.json()["detail"]

    def test_invalid_stp_returns_422(self, store: ReservationStore) -> None:
        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        app.state.callback_client = httpx.AsyncClient()
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        body = _reserve_request_body()
        body["criteria"]["p2ps"]["sourceSTP"] = "not-a-valid-stp"
        resp = client.post("/reservations", json=body)
        assert resp.status_code == 422

    def test_invalid_global_reservation_id_returns_422(self, store: ReservationStore) -> None:
        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        app.state.callback_client = httpx.AsyncClient()
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/reservations",
            json=_reserve_request_body(global_reservation_id="not-a-uuid"),
        )
        assert resp.status_code == 422

    def test_missing_description_returns_422(self, store: ReservationStore) -> None:
        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        app.state.callback_client = httpx.AsyncClient()
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        body = _reserve_request_body()
        del body["description"]
        resp = client.post("/reservations", json=body)
        assert resp.status_code == 422


class TestReserveAggregatorFailure:
    """Test when the aggregator is unreachable or returns errors."""

    def test_aggregator_unreachable_returns_502(self, store: ReservationStore) -> None:
        def failing_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(failing_handler))
        app.state.callback_client = httpx.AsyncClient()
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post("/reservations", json=_reserve_request_body())
        assert resp.status_code == 502

    def test_unexpected_sync_response_returns_502(self, store: ReservationStore) -> None:
        """Aggregator returns acknowledgment instead of reserveResponse."""

        def bad_handler(request: httpx.Request) -> httpx.Response:
            cid = parse_correlation_id(request.content)
            return httpx.Response(200, content=_acknowledgment_xml(cid))

        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(bad_handler))
        app.state.callback_client = httpx.AsyncClient()
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post("/reservations", json=_reserve_request_body())
        assert resp.status_code == 502
        assert "Unexpected" in resp.json()["detail"]


class TestReserveHappyPath:
    """Test full reserve flow: RESERVING → reserveConfirmed → reserveCommit → RESERVED."""

    @pytest.mark.anyio()
    async def test_reserve_success(self, store: ReservationStore) -> None:
        reserve_correlation_id: str = ""

        def nsi_handler(request: httpx.Request) -> httpx.Response:
            nonlocal reserve_correlation_id
            cid = parse_correlation_id(request.content)
            body = request.content.decode()
            if "queryNotificationSync" in body:
                return httpx.Response(200, content=build_query_notification_sync_response(cid))
            if "querySummarySync" in body:
                return httpx.Response(200, content=build_empty_query_summary_sync_response(cid))
            if "reserveCommit" in body:
                return httpx.Response(200, content=_acknowledgment_xml(cid))
            # First call is reserve → return reserveResponse
            reserve_correlation_id = cid
            return httpx.Response(200, content=_reserve_response_xml(cid))

        def callback_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(nsi_handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:
                    resp = await test_client.post("/reservations", json=_reserve_request_body())
                    assert resp.status_code == 202
                    body = resp.json()
                    assert body["instance"] == "/reservations/agg-conn-001"

                    await asyncio.sleep(0.05)
                    assert store.get("agg-conn-001") is not None
                    assert store.get("agg-conn-001").status == ReservationStatus.RESERVING  # type: ignore[union-attr]

                    # Get the correlation_id from the pending store
                    cid = _get_pending_correlation_id(store)

                    # Simulate reserveConfirmed callback
                    await test_client.post(
                        "/nsi/v2/callback",
                        content=_reserve_confirmed_xml(cid),
                    )
                    await asyncio.sleep(0.05)

                    # After reserveConfirmed, the background task sends reserveCommit
                    # and waits for reserveCommitConfirmed. Get the new pending correlation_id.
                    commit_cid = _get_pending_correlation_id(store)

                    # Simulate reserveCommitConfirmed callback
                    await test_client.post(
                        "/nsi/v2/callback",
                        content=_reserve_commit_confirmed_xml(commit_cid),
                    )
                    await asyncio.sleep(0.1)

                    assert store.get("agg-conn-001").status == ReservationStatus.RESERVED  # type: ignore[union-attr]


class TestReserveFailedCallback:
    """Test reserveFailed callback → FAILED."""

    @pytest.mark.anyio()
    async def test_reserve_failed(self, store: ReservationStore) -> None:
        def nsi_handler(request: httpx.Request) -> httpx.Response:
            cid = parse_correlation_id(request.content)
            return httpx.Response(200, content=_reserve_response_xml(cid))

        def callback_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(nsi_handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:
                    resp = await test_client.post("/reservations", json=_reserve_request_body())
                    assert resp.status_code == 202

                    await asyncio.sleep(0.05)
                    cid = _get_pending_correlation_id(store)

                    # Simulate reserveFailed callback
                    await test_client.post(
                        "/nsi/v2/callback",
                        content=_reserve_failed_xml(cid),
                    )
                    await asyncio.sleep(0.1)

                    reservation = store.get("agg-conn-001")
                    assert reservation is not None
                    assert reservation.status == ReservationStatus.FAILED
                    assert reservation.last_error is not None
                    assert "CAPACITY_UNAVAILABLE" in reservation.last_error


class TestReserveTimeoutCallback:
    """Test reserveTimeout callback → FAILED."""

    @pytest.mark.anyio()
    async def test_reserve_timeout(self, store: ReservationStore) -> None:
        def nsi_handler(request: httpx.Request) -> httpx.Response:
            cid = parse_correlation_id(request.content)
            return httpx.Response(200, content=_reserve_response_xml(cid))

        def callback_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(nsi_handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:
                    resp = await test_client.post("/reservations", json=_reserve_request_body())
                    assert resp.status_code == 202

                    await asyncio.sleep(0.05)
                    cid = _get_pending_correlation_id(store)

                    # Simulate reserveTimeout callback
                    await test_client.post(
                        "/nsi/v2/callback",
                        content=_reserve_timeout_xml(cid),
                    )
                    await asyncio.sleep(0.1)

                    reservation = store.get("agg-conn-001")
                    assert reservation is not None
                    assert reservation.status == ReservationStatus.FAILED
                    assert reservation.last_error is not None
                    assert "reserveTimeout" in reservation.last_error


class TestReserveNsiTimeout:
    """Test no callback at all → timeout → FAILED."""

    @pytest.mark.anyio()
    async def test_nsi_timeout(self, store: ReservationStore, monkeypatch: pytest.MonkeyPatch) -> None:
        from aggregator_proxy import settings as settings_module

        monkeypatch.setattr(settings_module.settings, "nsi_timeout", 0.1)

        def nsi_handler(request: httpx.Request) -> httpx.Response:
            cid = parse_correlation_id(request.content)
            return httpx.Response(200, content=_reserve_response_xml(cid))

        def callback_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(nsi_handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:
                    resp = await test_client.post("/reservations", json=_reserve_request_body())
                    assert resp.status_code == 202

                    # Wait for timeout
                    await asyncio.sleep(0.3)

                    reservation = store.get("agg-conn-001")
                    assert reservation is not None
                    assert reservation.status == ReservationStatus.FAILED


class TestReserveWithGlobalReservationId:
    """Test reserve with optional globalReservationId."""

    @pytest.mark.anyio()
    async def test_global_reservation_id_stored(self, store: ReservationStore) -> None:
        def nsi_handler(request: httpx.Request) -> httpx.Response:
            cid = parse_correlation_id(request.content)
            return httpx.Response(200, content=_reserve_response_xml(cid))

        def callback_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(nsi_handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:
                    resp = await test_client.post(
                        "/reservations",
                        json=_reserve_request_body(
                            global_reservation_id="urn:uuid:550e8400-e29b-41d4-a716-446655440000"
                        ),
                    )
                    assert resp.status_code == 202

                    await asyncio.sleep(0.05)
                    reservation = store.get("agg-conn-001")
                    assert reservation is not None
                    assert reservation.global_reservation_id == "urn:uuid:550e8400-e29b-41d4-a716-446655440000"
