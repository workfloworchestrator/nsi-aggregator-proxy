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


"""Tests for POST /reservations/{connectionId}/release."""

import asyncio
from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from aggregator_proxy.main import app
from aggregator_proxy.models import ReservationStatus
from aggregator_proxy.nsi_soap import parse_correlation_id
from aggregator_proxy.reservation_store import Reservation, ReservationStore
from tests.conftest import (
    build_acknowledgment_xml,
    build_empty_query_summary_sync_response,
    build_query_notification_sync_response,
    build_query_summary_sync_response,
    build_soap_envelope,
    get_pending_correlation_id,
    make_reservation,
)

CALLBACK_URL = "http://callback.example.com/result"
CONNECTION_ID = "test-conn-456"


def _release_confirmed_xml(correlation_id: str) -> bytes:
    return build_soap_envelope(
        f"<releaseConfirmed><connectionId>{CONNECTION_ID}</connectionId></releaseConfirmed>",
        correlation_id=correlation_id,
    )


def _data_plane_state_change_xml(
    active: bool = False,
    correlation_id: str = "urn:uuid:aggregator-generated-id",
) -> bytes:
    return build_soap_envelope(
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


def _make_reservation(status: ReservationStatus = ReservationStatus.ACTIVATED) -> Reservation:
    return make_reservation(connection_id=CONNECTION_ID, status=status, callback_url=CALLBACK_URL)


@pytest.fixture()
def _app_state(store: ReservationStore) -> None:
    """Inject test state into the FastAPI app."""
    app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(_nsi_handler))
    app.state.callback_client = httpx.AsyncClient()
    app.state.reservation_store = store


@pytest.fixture()
def client(_app_state: None) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _make_nsi_handler(
    provision_state: str = "Provisioned",
    data_plane_active: bool = True,
) -> Callable[[httpx.Request], httpx.Response]:
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
        return httpx.Response(200, content=build_acknowledgment_xml(cid))

    return handler


def _nsi_handler(request: httpx.Request) -> httpx.Response:
    return _make_nsi_handler()(request)


def _callback_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200)


class TestReleaseValidation:
    """Test request validation (404, 409)."""

    def test_release_unknown_connection_returns_404(self, client: TestClient) -> None:
        resp = client.post(
            "/reservations/nonexistent/release",
            json={"callbackURL": CALLBACK_URL},
        )
        assert resp.status_code == 404

    def test_release_non_activated_state_returns_409(self, store: ReservationStore) -> None:
        store.create(_make_reservation(status=ReservationStatus.RESERVED))
        # Mock returns Released+inactive → RESERVED, so release is rejected
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_make_nsi_handler(provision_state="Released", data_plane_active=False))
        )
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            f"/reservations/{CONNECTION_ID}/release",
            json={"callbackURL": CALLBACK_URL},
        )
        assert resp.status_code == 409


class TestReleaseAggregatorFailure:
    """Test when the aggregator is unreachable or returns errors."""

    def test_aggregator_unreachable_returns_502(self, store: ReservationStore) -> None:
        """ConnectError when sending the release request → 502."""
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
                    content=build_query_summary_sync_response(
                        connection_id=CONNECTION_ID,
                        correlation_id=cid,
                        provision_state="Provisioned",
                        data_plane_active=True,
                    ),
                )
            # release call → fail
            call_count += 1
            raise httpx.ConnectError("connection refused")

        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.state.callback_client = httpx.AsyncClient()
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(f"/reservations/{CONNECTION_ID}/release", json={"callbackURL": CALLBACK_URL})
        assert resp.status_code == 502
        assert call_count == 1

    def test_unexpected_sync_response_returns_502(self, store: ReservationStore) -> None:
        """Aggregator returns a non-Acknowledgment for release → 502."""
        store.create(_make_reservation())

        def handler(request: httpx.Request) -> httpx.Response:
            cid = parse_correlation_id(request.content)
            body = request.content.decode()
            if "queryNotificationSync" in body:
                return httpx.Response(200, content=build_query_notification_sync_response(cid))
            if "querySummarySync" in body:
                return httpx.Response(
                    200,
                    content=build_query_summary_sync_response(
                        connection_id=CONNECTION_ID,
                        correlation_id=cid,
                        provision_state="Provisioned",
                        data_plane_active=True,
                    ),
                )
            body = "<reserveResponse><connectionId>wrong</connectionId></reserveResponse>"
            return httpx.Response(200, content=build_soap_envelope(body, cid))

        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.state.callback_client = httpx.AsyncClient()
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(f"/reservations/{CONNECTION_ID}/release", json={"callbackURL": CALLBACK_URL})
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

        resp = client.post(f"/reservations/{CONNECTION_ID}/release", json={"callbackURL": CALLBACK_URL})
        assert resp.status_code == 502


class TestReleaseHappyPath:
    """Test successful release flow: ACTIVATED → DEACTIVATING → RESERVED."""

    @pytest.mark.anyio()
    async def test_release_success(self, store: ReservationStore) -> None:
        store.create(_make_reservation())

        async with httpx.AsyncClient(transport=httpx.MockTransport(_nsi_handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(_callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:
                    resp = await test_client.post(
                        f"/reservations/{CONNECTION_ID}/release",
                        json={"callbackURL": CALLBACK_URL},
                    )
                    assert resp.status_code == 202

                    await asyncio.sleep(0.05)
                    assert store.get(CONNECTION_ID).status == ReservationStatus.DEACTIVATING  # type: ignore[union-attr]

                    cid = get_pending_correlation_id(store)

                    # Simulate releaseConfirmed callback
                    await test_client.post("/nsi/v2/callback", content=_release_confirmed_xml(cid))
                    await asyncio.sleep(0.05)

                    # Simulate dataPlaneStateChange active=False
                    await test_client.post(
                        "/nsi/v2/callback",
                        content=_data_plane_state_change_xml(active=False),
                    )
                    await asyncio.sleep(0.1)

                    assert store.get(CONNECTION_ID).status == ReservationStatus.RESERVED  # type: ignore[union-attr]


class TestReleaseDataPlaneActiveThenInactive:
    """DataPlaneStateChange active=True followed by active=False → RESERVED."""

    @pytest.mark.anyio()
    async def test_active_then_inactive(self, store: ReservationStore) -> None:
        store.create(_make_reservation())

        async with httpx.AsyncClient(transport=httpx.MockTransport(_nsi_handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(_callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:
                    resp = await test_client.post(
                        f"/reservations/{CONNECTION_ID}/release",
                        json={"callbackURL": CALLBACK_URL},
                    )
                    assert resp.status_code == 202
                    await asyncio.sleep(0.05)

                    cid = get_pending_correlation_id(store)

                    # releaseConfirmed
                    await test_client.post("/nsi/v2/callback", content=_release_confirmed_xml(cid))
                    await asyncio.sleep(0.05)

                    # dataPlaneStateChange active=True (spurious, should be ignored)
                    await test_client.post(
                        "/nsi/v2/callback",
                        content=_data_plane_state_change_xml(active=True),
                    )
                    await asyncio.sleep(0.05)

                    assert store.get(CONNECTION_ID).status == ReservationStatus.DEACTIVATING  # type: ignore[union-attr]

                    # dataPlaneStateChange active=False
                    await test_client.post(
                        "/nsi/v2/callback",
                        content=_data_plane_state_change_xml(active=False),
                    )
                    await asyncio.sleep(0.1)

                    assert store.get(CONNECTION_ID).status == ReservationStatus.RESERVED  # type: ignore[union-attr]


class TestReleaseTimeout:
    """Test timeout scenarios."""

    @pytest.mark.anyio()
    async def test_release_confirmed_timeout(self, store: ReservationStore, monkeypatch: pytest.MonkeyPatch) -> None:
        """No releaseConfirmed → FAILED."""
        from aggregator_proxy import settings as settings_module

        monkeypatch.setattr(settings_module.settings, "nsi_timeout", 0.1)

        store.create(_make_reservation())

        async with httpx.AsyncClient(transport=httpx.MockTransport(_nsi_handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(_callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:
                    resp = await test_client.post(
                        f"/reservations/{CONNECTION_ID}/release",
                        json={"callbackURL": CALLBACK_URL},
                    )
                    assert resp.status_code == 202

                    await asyncio.sleep(0.3)

                    assert store.get(CONNECTION_ID).status == ReservationStatus.FAILED  # type: ignore[union-attr]

    @pytest.mark.anyio()
    async def test_dataplane_timeout(self, store: ReservationStore, monkeypatch: pytest.MonkeyPatch) -> None:
        """ReleaseConfirmed received but no DataPlaneStateChange → FAILED."""
        from aggregator_proxy import settings as settings_module

        monkeypatch.setattr(settings_module.settings, "nsi_timeout", 5)
        monkeypatch.setattr(settings_module.settings, "dataplane_timeout", 0.1)

        store.create(_make_reservation())

        async with httpx.AsyncClient(transport=httpx.MockTransport(_nsi_handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(_callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:
                    resp = await test_client.post(
                        f"/reservations/{CONNECTION_ID}/release",
                        json={"callbackURL": CALLBACK_URL},
                    )
                    assert resp.status_code == 202
                    await asyncio.sleep(0.05)

                    cid = get_pending_correlation_id(store)

                    # Send releaseConfirmed
                    await test_client.post("/nsi/v2/callback", content=_release_confirmed_xml(cid))

                    await asyncio.sleep(0.3)

                    assert store.get(CONNECTION_ID).status == ReservationStatus.FAILED  # type: ignore[union-attr]
