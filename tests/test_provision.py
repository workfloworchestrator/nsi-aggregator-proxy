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


"""Tests for POST /reservations/{connectionId}/provision."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from aggregator_proxy.main import app
from aggregator_proxy.models import P2PS, CriteriaResponse, ReservationStatus
from aggregator_proxy.nsi_soap import parse_correlation_id
from aggregator_proxy.nsi_soap.namespaces import NSMAP
from aggregator_proxy.reservation_store import Reservation, ReservationStore
from tests.conftest import (
    build_empty_query_summary_sync_response,
    build_query_notification_sync_response,
    build_query_summary_sync_response,
)

_C = NSMAP["nsi_ctypes"]
_H = NSMAP["nsi_headers"]
_S = NSMAP["soapenv"]

CALLBACK_URL = "http://callback.example.com/result"
CONNECTION_ID = "test-conn-123"


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
    """SOAP acknowledgment response (sync reply to provision)."""
    return _make_soap("<acknowledgment/>", correlation_id)


def _provision_confirmed_xml(correlation_id: str) -> bytes:
    return _make_soap(
        f"<provisionConfirmed><connectionId>{CONNECTION_ID}</connectionId></provisionConfirmed>",
        correlation_id=correlation_id,
    )


def _data_plane_state_change_xml(
    active: bool = True,
    correlation_id: str = "urn:uuid:aggregator-generated-id",
) -> bytes:
    return _make_soap(
        f"""\
<dataPlaneStateChange>
  <connectionId>{CONNECTION_ID}</connectionId>
  <notificationId>1</notificationId>
  <timeStamp>2025-01-01T00:00:00Z</timeStamp>
  <dataPlaneStatus>
    <active>{"true" if active else "false"}</active>
    <version>1</version>
    <versionConsistent>true</versionConsistent>
  </dataPlaneStatus>
</dataPlaneStateChange>""",
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


def _make_nsi_handler(
    provision_state: str = "Released",
    data_plane_active: bool = False,
) -> object:
    """Create a mock NSI handler that returns the given states for querySummarySync."""

    def handler(request: httpx.Request) -> httpx.Response:
        cid = parse_correlation_id(request.content)
        body = request.content.decode()
        if "queryNotificationSync" in body:
            return httpx.Response(200, content=build_query_notification_sync_response(cid))
        if "querySummarySync" in body:
            if CONNECTION_ID in body:
                return httpx.Response(
                    200,
                    content=build_query_summary_sync_response(
                        connection_id=CONNECTION_ID,
                        correlation_id=cid,
                        provision_state=provision_state,
                        data_plane_active=data_plane_active,
                    ),
                )
            return httpx.Response(200, content=build_empty_query_summary_sync_response(cid))
        return httpx.Response(200, content=_acknowledgment_xml(cid))

    return handler


@pytest.fixture()
def _app_state(store: ReservationStore) -> None:
    """Inject test state into the FastAPI app."""
    app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(_make_nsi_handler()))
    app.state.callback_client = httpx.AsyncClient()
    app.state.reservation_store = store


@pytest.fixture()
def client(_app_state: None) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


class TestProvisionValidation:
    """Test request validation (404, 409)."""

    def test_provision_unknown_connection_returns_404(self, client: TestClient) -> None:
        resp = client.post(
            "/reservations/nonexistent/provision",
            json={"callbackURL": CALLBACK_URL},
        )
        assert resp.status_code == 404

    def test_provision_non_reserved_state_returns_409(self, store: ReservationStore) -> None:
        store.create(_make_reservation(status=ReservationStatus.ACTIVATING))
        # Mock returns Provisioned+inactive → ACTIVATING, so provision is rejected
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_make_nsi_handler(provision_state="Provisioned"))
        )
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            f"/reservations/{CONNECTION_ID}/provision",
            json={"callbackURL": CALLBACK_URL},
        )
        assert resp.status_code == 409


class TestProvisionAggregatorFailure:
    """Test when the aggregator is unreachable or returns errors."""

    def test_aggregator_unreachable_returns_502(self, store: ReservationStore) -> None:
        """ConnectError when sending the provision request → 502."""
        store.create(_make_reservation())
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            cid = parse_correlation_id(request.content)
            body = request.content.decode()
            if "queryNotificationSync" in body:
                return httpx.Response(200, content=build_query_notification_sync_response(cid))
            if "querySummarySync" in body:
                return httpx.Response(
                    200,
                    content=build_query_summary_sync_response(connection_id=CONNECTION_ID, correlation_id=cid),
                )
            # provision call → fail
            call_count += 1
            raise httpx.ConnectError("connection refused")

        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.state.callback_client = httpx.AsyncClient()
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(f"/reservations/{CONNECTION_ID}/provision", json={"callbackURL": CALLBACK_URL})
        assert resp.status_code == 502
        assert call_count == 1

    def test_unexpected_sync_response_returns_502(self, store: ReservationStore) -> None:
        """Aggregator returns a non-Acknowledgment for provision → 502."""
        store.create(_make_reservation())

        def handler(request: httpx.Request) -> httpx.Response:
            cid = parse_correlation_id(request.content)
            body = request.content.decode()
            if "queryNotificationSync" in body:
                return httpx.Response(200, content=build_query_notification_sync_response(cid))
            if "querySummarySync" in body:
                return httpx.Response(
                    200,
                    content=build_query_summary_sync_response(connection_id=CONNECTION_ID, correlation_id=cid),
                )
            # Return a reserveResponse instead of acknowledgment
            return httpx.Response(
                200,
                content=_make_soap("<reserveResponse><connectionId>wrong</connectionId></reserveResponse>", cid),
            )

        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.state.callback_client = httpx.AsyncClient()
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(f"/reservations/{CONNECTION_ID}/provision", json={"callbackURL": CALLBACK_URL})
        assert resp.status_code == 502
        assert "Unexpected" in resp.json()["detail"]

    def test_refresh_unreachable_returns_502(self, store: ReservationStore) -> None:
        """Aggregator unreachable during pre-operation refresh → 502."""
        store.create(_make_reservation())

        def failing_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(failing_handler))
        app.state.callback_client = httpx.AsyncClient()
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(f"/reservations/{CONNECTION_ID}/provision", json={"callbackURL": CALLBACK_URL})
        assert resp.status_code == 502


class TestProvisionHappyPath:
    """Test successful provision flow: RESERVED → ACTIVATING → ACTIVATED."""

    _captured_correlation_id: str = ""

    @pytest.mark.anyio()
    async def test_provision_success(self, store: ReservationStore) -> None:
        store.create(_make_reservation())

        async with httpx.AsyncClient(transport=httpx.MockTransport(self._nsi_handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(self._callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:
                    resp = await test_client.post(
                        f"/reservations/{CONNECTION_ID}/provision",
                        json={"callbackURL": CALLBACK_URL},
                    )
                    assert resp.status_code == 202

                    await asyncio.sleep(0.05)
                    assert store.get(CONNECTION_ID) is not None
                    assert store.get(CONNECTION_ID).status == ReservationStatus.ACTIVATING  # type: ignore[union-attr]

                    # Get the actual correlation_id registered by the provision endpoint
                    cid = _get_pending_correlation_id(store)

                    # Simulate provisionConfirmed callback
                    await test_client.post(
                        "/nsi/v2/callback",
                        content=_provision_confirmed_xml(cid),
                    )
                    await asyncio.sleep(0.05)

                    # Simulate dataPlaneStateChange active=True
                    await test_client.post(
                        "/nsi/v2/callback",
                        content=_data_plane_state_change_xml(active=True),
                    )
                    await asyncio.sleep(0.1)

                    assert store.get(CONNECTION_ID).status == ReservationStatus.ACTIVATED  # type: ignore[union-attr]

    def _nsi_handler(self, request: httpx.Request) -> httpx.Response:
        cid = parse_correlation_id(request.content)
        body = request.content.decode()
        if "queryNotificationSync" in body:
            return httpx.Response(200, content=build_query_notification_sync_response(cid))
        if "querySummarySync" in body:
            return httpx.Response(
                200,
                content=build_query_summary_sync_response(connection_id=CONNECTION_ID, correlation_id=cid),
            )
        self.__class__._captured_correlation_id = cid
        return httpx.Response(200, content=_acknowledgment_xml(cid))

    @staticmethod
    def _callback_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)


class TestProvisionDataPlaneInactiveThenActive:
    """DataPlaneStateChange active=False followed by active=True → ACTIVATED."""

    @pytest.mark.anyio()
    async def test_inactive_then_active(self, store: ReservationStore) -> None:
        store.create(_make_reservation())

        async with httpx.AsyncClient(transport=httpx.MockTransport(self._nsi_handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(self._callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:
                    resp = await test_client.post(
                        f"/reservations/{CONNECTION_ID}/provision",
                        json={"callbackURL": CALLBACK_URL},
                    )
                    assert resp.status_code == 202
                    await asyncio.sleep(0.05)

                    cid = _get_pending_correlation_id(store)

                    # provisionConfirmed
                    await test_client.post("/nsi/v2/callback", content=_provision_confirmed_xml(cid))
                    await asyncio.sleep(0.05)

                    # dataPlaneStateChange active=False
                    await test_client.post(
                        "/nsi/v2/callback",
                        content=_data_plane_state_change_xml(active=False),
                    )
                    await asyncio.sleep(0.05)

                    # Should still be ACTIVATING (not yet active)
                    assert store.get(CONNECTION_ID).status == ReservationStatus.ACTIVATING  # type: ignore[union-attr]

                    # dataPlaneStateChange active=True
                    await test_client.post(
                        "/nsi/v2/callback",
                        content=_data_plane_state_change_xml(active=True),
                    )
                    await asyncio.sleep(0.1)

                    assert store.get(CONNECTION_ID).status == ReservationStatus.ACTIVATED  # type: ignore[union-attr]

    @staticmethod
    def _nsi_handler(request: httpx.Request) -> httpx.Response:
        return _make_nsi_handler()(request)  # type: ignore[operator]

    @staticmethod
    def _callback_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)


class TestProvisionTimeout:
    """Test timeout scenarios."""

    @pytest.mark.anyio()
    async def test_provision_confirmed_timeout(self, store: ReservationStore, monkeypatch: pytest.MonkeyPatch) -> None:
        """No provisionConfirmed → FAILED."""
        from aggregator_proxy import settings as settings_module

        monkeypatch.setattr(settings_module.settings, "nsi_timeout", 0.1)

        store.create(_make_reservation())

        async with httpx.AsyncClient(transport=httpx.MockTransport(self._nsi_handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(self._callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:
                    resp = await test_client.post(
                        f"/reservations/{CONNECTION_ID}/provision",
                        json={"callbackURL": CALLBACK_URL},
                    )
                    assert resp.status_code == 202

                    # Wait for timeout to trigger
                    await asyncio.sleep(0.3)

                    assert store.get(CONNECTION_ID).status == ReservationStatus.FAILED  # type: ignore[union-attr]

    @pytest.mark.anyio()
    async def test_dataplane_timeout(self, store: ReservationStore, monkeypatch: pytest.MonkeyPatch) -> None:
        """ProvisionConfirmed received but no DataPlaneStateChange → FAILED."""
        from aggregator_proxy import settings as settings_module

        monkeypatch.setattr(settings_module.settings, "nsi_timeout", 5)
        monkeypatch.setattr(settings_module.settings, "dataplane_timeout", 0.1)

        store.create(_make_reservation())

        async with httpx.AsyncClient(transport=httpx.MockTransport(self._nsi_handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(self._callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:
                    resp = await test_client.post(
                        f"/reservations/{CONNECTION_ID}/provision",
                        json={"callbackURL": CALLBACK_URL},
                    )
                    assert resp.status_code == 202
                    await asyncio.sleep(0.05)

                    cid = _get_pending_correlation_id(store)

                    # Send provisionConfirmed
                    await test_client.post("/nsi/v2/callback", content=_provision_confirmed_xml(cid))

                    # Wait for dataplane timeout
                    await asyncio.sleep(0.3)

                    assert store.get(CONNECTION_ID).status == ReservationStatus.FAILED  # type: ignore[union-attr]

    @staticmethod
    def _nsi_handler(request: httpx.Request) -> httpx.Response:
        return _make_nsi_handler()(request)  # type: ignore[operator]

    @staticmethod
    def _callback_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)
