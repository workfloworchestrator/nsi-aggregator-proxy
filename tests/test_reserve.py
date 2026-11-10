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
import json
from collections.abc import Callable
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from lxml import etree

from aggregator_proxy.main import app
from aggregator_proxy.models import ReservationStatus
from aggregator_proxy.nsi_soap import parse_correlation_id
from aggregator_proxy.nsi_soap.namespaces import NSMAP
from aggregator_proxy.reservation_store import Operation, ReservationStore
from aggregator_proxy.settings import settings
from tests.conftest import (
    build_acknowledgment_xml,
    build_empty_query_summary_sync_response,
    build_message_delivery_timeout_xml,
    build_query_notification_sync_response,
    build_query_summary_sync_response,
    build_reserve_request_body,
    build_reserve_timeout_xml,
    build_soap_envelope,
    get_pending_correlation_id,
    utc_now_iso,
    wait_until,
)

_P = NSMAP["nsi_p2p"]

CALLBACK_URL = "http://callback.example.com/result"


def _reserve_response_xml(correlation_id: str, connection_id: str = "agg-conn-001") -> bytes:
    return build_soap_envelope(
        f"<reserveResponse><connectionId>{connection_id}</connectionId></reserveResponse>",
        correlation_id,
    )


def _reserve_confirmed_xml(correlation_id: str, connection_id: str = "agg-conn-001") -> bytes:
    return build_soap_envelope(
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
    return build_soap_envelope(
        f"<reserveCommitConfirmed><connectionId>{connection_id}</connectionId></reserveCommitConfirmed>",
        correlation_id,
    )


def _reserve_failed_xml(correlation_id: str, connection_id: str = "agg-conn-001") -> bytes:
    return build_soap_envelope(
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


def _reserve_commit_failed_xml(correlation_id: str, connection_id: str = "agg-conn-001") -> bytes:
    return build_soap_envelope(
        f"""\
<reserveCommitFailed>
  <connectionId>{connection_id}</connectionId>
  <serviceException>
    <nsaId>urn:ogf:network:child:2025:nsa</nsaId>
    <connectionId>{connection_id}</connectionId>
    <errorId>00999</errorId>
    <text>COMMIT_FAILED</text>
  </serviceException>
</reserveCommitFailed>""",
        correlation_id,
    )


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
            json=build_reserve_request_body(provider_nsa="urn:ogf:network:wrong:2025:nsa:wrong"),
        )
        assert resp.status_code == 400
        assert "providerNSA" in resp.json()["detail"]

    def test_invalid_stp_returns_422(self, store: ReservationStore) -> None:
        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        app.state.callback_client = httpx.AsyncClient()
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        body = build_reserve_request_body()
        body["criteria"]["p2ps"]["sourceSTP"] = "not-a-valid-stp"
        resp = client.post("/reservations", json=body)
        assert resp.status_code == 422

    def test_invalid_ero_member_returns_422(self, store: ReservationStore) -> None:
        """An ERO member must be a NURN.

        Regression guard: the ero validator is separate from the sourceSTP/destSTP one, which would
        raise TypeError (a 500, not a 422) when handed a list.
        """
        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        app.state.callback_client = httpx.AsyncClient()
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        body = build_reserve_request_body()
        body["criteria"]["p2ps"]["ero"] = ["not-a-valid-stp"]
        resp = client.post("/reservations", json=body)
        assert resp.status_code == 422

    def test_invalid_global_reservation_id_returns_422(self, store: ReservationStore) -> None:
        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        app.state.callback_client = httpx.AsyncClient()
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/reservations",
            json=build_reserve_request_body(global_reservation_id="not-a-uuid"),
        )
        assert resp.status_code == 422

    def test_missing_description_returns_422(self, store: ReservationStore) -> None:
        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        app.state.callback_client = httpx.AsyncClient()
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        body = build_reserve_request_body()
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

        resp = client.post("/reservations", json=build_reserve_request_body())
        assert resp.status_code == 502

    def test_unexpected_sync_response_returns_502(self, store: ReservationStore) -> None:
        """Aggregator returns acknowledgment instead of reserveResponse."""

        def bad_handler(request: httpx.Request) -> httpx.Response:
            cid = parse_correlation_id(request.content)
            return httpx.Response(200, content=build_acknowledgment_xml(cid))

        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(bad_handler))
        app.state.callback_client = httpx.AsyncClient()
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post("/reservations", json=build_reserve_request_body())
        assert resp.status_code == 502
        assert "Unexpected" in resp.json()["detail"]


class TestReserveHappyPath:
    """Test full reserve flow: RESERVING → reserveConfirmed → reserveCommit → RESERVED."""

    @pytest.mark.anyio()
    async def test_ero_reaches_the_wire_but_not_the_response(self, store: ReservationStore) -> None:
        """The ERO is request-only: it goes out in the SOAP but is absent from the stored criteria."""
        reserve_xml: bytes = b""

        def nsi_handler(request: httpx.Request) -> httpx.Response:
            nonlocal reserve_xml
            cid = parse_correlation_id(request.content)
            body = request.content.decode()
            if "queryNotificationSync" in body:
                return httpx.Response(200, content=build_query_notification_sync_response(cid))
            if "querySummarySync" in body:
                return httpx.Response(200, content=build_empty_query_summary_sync_response(cid))
            reserve_xml = request.content
            return httpx.Response(200, content=_reserve_response_xml(cid))

        async with httpx.AsyncClient(transport=httpx.MockTransport(nsi_handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store

                request_body = build_reserve_request_body()
                request_body["criteria"]["p2ps"]["ero"] = [
                    "urn:ogf:network:a.net:2025:hop-1",
                    "urn:ogf:network:b.net:2025:hop-2",
                ]

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:
                    resp = await test_client.post("/reservations", json=request_body)
                    assert resp.status_code == 202
                    await asyncio.sleep(0.05)

        ordered = etree.fromstring(reserve_xml).findall(".//ero/orderedSTP")
        assert [element.findtext("stp") for element in ordered] == request_body["criteria"]["p2ps"]["ero"]

        reservation = store.get("agg-conn-001")
        assert reservation is not None
        assert reservation.criteria is not None
        assert reservation.criteria.p2ps.sourceSTP == request_body["criteria"]["p2ps"]["sourceSTP"]

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
                return httpx.Response(200, content=build_acknowledgment_xml(cid))
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
                    resp = await test_client.post("/reservations", json=build_reserve_request_body())
                    assert resp.status_code == 202
                    body = resp.json()
                    assert body["instance"] == "/reservations/agg-conn-001"

                    await asyncio.sleep(0.05)
                    assert store.get("agg-conn-001") is not None
                    assert store.get("agg-conn-001").status == ReservationStatus.RESERVING  # type: ignore[union-attr]
                    assert store.task_in_flight(Operation.RESERVE, "agg-conn-001")

                    # Get the correlation_id from the pending store
                    cid = get_pending_correlation_id(store)

                    # Simulate reserveConfirmed callback
                    await test_client.post(
                        "/nsi/v2/callback",
                        content=_reserve_confirmed_xml(cid),
                    )
                    await asyncio.sleep(0.05)

                    # After reserveConfirmed, the background task sends reserveCommit
                    # and waits for reserveCommitConfirmed. Get the new pending correlation_id.
                    commit_cid = get_pending_correlation_id(store)

                    # Simulate reserveCommitConfirmed callback
                    await test_client.post(
                        "/nsi/v2/callback",
                        content=_reserve_commit_confirmed_xml(commit_cid),
                    )
                    await asyncio.sleep(0.1)

                    assert store.get("agg-conn-001").status == ReservationStatus.RESERVED  # type: ignore[union-attr]
                    assert not store.task_in_flight(Operation.RESERVE, "agg-conn-001")


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
                    resp = await test_client.post("/reservations", json=build_reserve_request_body())
                    assert resp.status_code == 202

                    await asyncio.sleep(0.05)
                    cid = get_pending_correlation_id(store)

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
                    # Released on failure too, or the stuck reservation could never be terminated.
                    assert not store.task_in_flight(Operation.RESERVE, "agg-conn-001")


def _reserve_timeout(notification_id: int, timestamp: str | None = None) -> str:
    return build_reserve_timeout_xml("agg-conn-001", notification_id, timestamp or utc_now_iso())


def _delivery_timeout(notification_id: int, timestamp: str | None = None) -> str:
    return build_message_delivery_timeout_xml("agg-conn-001", notification_id, timestamp or utc_now_iso())


class TestReserveTimeoutNotifications:
    """A child that never answers: Safnari sends both notifications, in either order, with its own correlationIds.

    They end no wait; when it runs out, the reason is the latest one raised since the wait began.
    """

    @pytest.mark.anyio()
    @pytest.mark.parametrize(
        ("notifications", "expected_error"),
        [
            pytest.param(
                lambda: [_reserve_timeout(1), _delivery_timeout(2)],
                "messageDeliveryTimeout",
                id="reserve-timeout-first",
            ),
            pytest.param(
                lambda: [_delivery_timeout(1), _reserve_timeout(2)], "reserveTimeout", id="delivery-timeout-first"
            ),
            # Raised before this reserve was sent: not its reason.
            pytest.param(
                lambda: [_delivery_timeout(1, "2025-06-01T12:00:00Z")],
                "no reserveConfirmed received within timeout",
                id="older-notification-ignored",
            ),
            pytest.param(lambda: [], "no reserveConfirmed received within timeout", id="none"),
        ],
    )
    async def test_fails_the_reserve(
        self,
        store: ReservationStore,
        monkeypatch: pytest.MonkeyPatch,
        notifications: Callable[[], list[str]],
        expected_error: str,
    ) -> None:
        monkeypatch.setattr(settings, "nsi_timeout", 0.2)
        listed: list[str] = []

        def nsi_handler(request: httpx.Request) -> httpx.Response:
            cid = parse_correlation_id(request.content)
            if "queryNotificationSync" in request.content.decode():
                return httpx.Response(200, content=build_query_notification_sync_response(cid, *listed))
            return httpx.Response(200, content=_reserve_response_xml(cid))

        callbacks: list[dict[str, object]] = []

        def callback_handler(request: httpx.Request) -> httpx.Response:
            callbacks.append(json.loads(request.content))
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(nsi_handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:
                    resp = await test_client.post("/reservations", json=build_reserve_request_body())
                    assert resp.status_code == 202
                    sent = notifications()
                    listed.extend(sent)
                    acks = [
                        await test_client.post(
                            "/nsi/v2/callback", content=build_soap_envelope(notification, f"urn:uuid:{uuid4()}")
                        )
                        for notification in sent
                    ]
                    assert callbacks == [], "a timeout notification ended the wait"
                    await wait_until(lambda: bool(callbacks))

        assert all(ack.status_code == 200 for ack in acks)
        reservation = store.get("agg-conn-001")
        assert reservation is not None
        assert reservation.status == ReservationStatus.FAILED
        assert (reservation.last_error or "").startswith(expected_error)
        assert [callback["status"] for callback in callbacks] == ["FAILED"]
        assert not store.task_in_flight(Operation.RESERVE, "agg-conn-001")


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
                    resp = await test_client.post("/reservations", json=build_reserve_request_body())
                    assert resp.status_code == 202

                    # Wait for timeout
                    await asyncio.sleep(0.3)

                    reservation = store.get("agg-conn-001")
                    assert reservation is not None
                    assert reservation.status == ReservationStatus.FAILED


class TestReserveCommitFailed:
    """Test reserveCommitFailed callback → FAILED."""

    @pytest.mark.anyio()
    async def test_reserve_commit_failed(self, store: ReservationStore) -> None:
        def nsi_handler(request: httpx.Request) -> httpx.Response:
            cid = parse_correlation_id(request.content)
            body = request.content.decode()
            if "queryNotificationSync" in body:
                return httpx.Response(200, content=build_query_notification_sync_response(cid))
            if "querySummarySync" in body:
                return httpx.Response(200, content=build_empty_query_summary_sync_response(cid))
            if "reserveCommit" in body:
                return httpx.Response(200, content=build_acknowledgment_xml(cid))
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
                    resp = await test_client.post("/reservations", json=build_reserve_request_body())
                    assert resp.status_code == 202

                    await asyncio.sleep(0.05)
                    cid = get_pending_correlation_id(store)

                    # Simulate reserveConfirmed callback
                    await test_client.post("/nsi/v2/callback", content=_reserve_confirmed_xml(cid))
                    await asyncio.sleep(0.05)

                    # Get commit correlation_id
                    commit_cid = get_pending_correlation_id(store)

                    # Simulate reserveCommitFailed callback
                    await test_client.post(
                        "/nsi/v2/callback",
                        content=_reserve_commit_failed_xml(commit_cid),
                    )
                    await asyncio.sleep(0.1)

                    reservation = store.get("agg-conn-001")
                    assert reservation is not None
                    assert reservation.status == ReservationStatus.FAILED
                    assert reservation.last_error is not None
                    assert "reserveCommitFailed" in reservation.last_error


class TestReserveCommitAggregatorUnreachable:
    """Test aggregator unreachable when sending reserveCommit → FAILED."""

    @pytest.mark.anyio()
    async def test_reserve_commit_aggregator_unreachable(self, store: ReservationStore) -> None:
        commit_should_fail = False

        def nsi_handler(request: httpx.Request) -> httpx.Response:
            if commit_should_fail:
                raise httpx.ConnectError("connection refused")
            cid = parse_correlation_id(request.content)
            body = request.content.decode()
            if "queryNotificationSync" in body:
                return httpx.Response(200, content=build_query_notification_sync_response(cid))
            if "querySummarySync" in body:
                return httpx.Response(200, content=build_empty_query_summary_sync_response(cid))
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
                    resp = await test_client.post("/reservations", json=build_reserve_request_body())
                    assert resp.status_code == 202

                    await asyncio.sleep(0.05)
                    cid = get_pending_correlation_id(store)

                    # Make subsequent NSI calls fail
                    commit_should_fail = True

                    # Simulate reserveConfirmed callback → triggers reserveCommit which will fail
                    await test_client.post("/nsi/v2/callback", content=_reserve_confirmed_xml(cid))
                    await asyncio.sleep(0.1)

                    reservation = store.get("agg-conn-001")
                    assert reservation is not None
                    assert reservation.status == ReservationStatus.FAILED
                    assert reservation.last_error is not None
                    assert "failed to send reserveCommit" in reservation.last_error


class TestReserveCommitTimeout:
    """Test no reserveCommitConfirmed received → FAILED."""

    @pytest.mark.anyio()
    async def test_reserve_commit_timeout(self, store: ReservationStore, monkeypatch: pytest.MonkeyPatch) -> None:
        from aggregator_proxy import settings as settings_module

        monkeypatch.setattr(settings_module.settings, "nsi_timeout", 0.1)

        def nsi_handler(request: httpx.Request) -> httpx.Response:
            cid = parse_correlation_id(request.content)
            body = request.content.decode()
            if "queryNotificationSync" in body:
                return httpx.Response(200, content=build_query_notification_sync_response(cid))
            if "querySummarySync" in body:
                return httpx.Response(200, content=build_empty_query_summary_sync_response(cid))
            if "reserveCommit" in body:
                return httpx.Response(200, content=build_acknowledgment_xml(cid))
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
                    resp = await test_client.post("/reservations", json=build_reserve_request_body())
                    assert resp.status_code == 202

                    await asyncio.sleep(0.05)
                    cid = get_pending_correlation_id(store)

                    # Simulate reserveConfirmed → triggers reserveCommit
                    await test_client.post("/nsi/v2/callback", content=_reserve_confirmed_xml(cid))

                    # Wait for commit timeout (don't send reserveCommitConfirmed)
                    await asyncio.sleep(0.3)

                    reservation = store.get("agg-conn-001")
                    assert reservation is not None
                    assert reservation.status == ReservationStatus.FAILED
                    assert reservation.last_error is not None
                    assert "no reserveCommitConfirmed" in reservation.last_error


class TestReserveWithGlobalReservationId:
    """Test reserve with optional globalReservationId."""

    @pytest.mark.anyio()
    async def test_global_reservation_id_stored(self, store: ReservationStore) -> None:
        def nsi_handler(request: httpx.Request) -> httpx.Response:
            cid = parse_correlation_id(request.content)
            body = request.content.decode()
            # The idempotency pre-query finds no existing reservation for this globalReservationId.
            if "querySummarySync" in body:
                return httpx.Response(200, content=build_empty_query_summary_sync_response(cid))
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
                        json=build_reserve_request_body(
                            global_reservation_id="urn:uuid:550e8400-e29b-41d4-a716-446655440000"
                        ),
                    )
                    assert resp.status_code == 202

                    await asyncio.sleep(0.05)
                    reservation = store.get("agg-conn-001")
                    assert reservation is not None
                    assert reservation.global_reservation_id == "urn:uuid:550e8400-e29b-41d4-a716-446655440000"

    @pytest.mark.anyio()
    @pytest.mark.parametrize(
        ("lifecycle_state", "reused"),
        [
            pytest.param("Created", True, id="live-reused"),
            # On its way out: reusing it would hand the terminate's result to the reserve's caller.
            pytest.param("Terminating", False, id="terminating-not-reused"),
            pytest.param("Terminated", False, id="terminated-not-reused"),
        ],
    )
    async def test_duplicate_reserve_reuses_existing(
        self, store: ReservationStore, lifecycle_state: str, reused: bool
    ) -> None:
        """A retried reserve with the same globalReservationId reuses the aggregator's live reservation."""
        grid = "urn:uuid:550e8400-e29b-41d4-a716-446655440000"
        new_callback = "http://callback.example.com/retry"
        delivered: list[str] = []
        reserve_sent = False

        def nsi_handler(request: httpx.Request) -> httpx.Response:
            nonlocal reserve_sent
            cid = parse_correlation_id(request.content)
            body = request.content.decode()
            if "queryNotificationSync" in body:
                return httpx.Response(200, content=build_query_notification_sync_response(cid))
            if "querySummarySync" in body:
                # The aggregator already holds a reservation for this globalReservationId.
                return httpx.Response(
                    200,
                    content=build_query_summary_sync_response(
                        connection_id="agg-conn-001",
                        correlation_id=cid,
                        global_reservation_id=grid,
                        lifecycle_state=lifecycle_state,
                    ),
                )
            reserve_sent = True
            return httpx.Response(200, content=_reserve_response_xml(cid, connection_id="agg-conn-002"))

        def callback_handler(request: httpx.Request) -> httpx.Response:
            delivered.append(str(request.url))
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
                        json=build_reserve_request_body(global_reservation_id=grid, callback_url=new_callback),
                    )
                    await asyncio.sleep(0.1)  # let the background re-delivery run while cb_client is open

        assert resp.status_code == 202
        assert resp.json()["instance"] == f"/reservations/agg-conn-00{1 if reused else 2}"
        assert reserve_sent is not reused
        # A reused, settled (RESERVED) reservation is re-delivered to the new url; a new reserve is still running.
        assert delivered == ([new_callback] if reused else [])
        # No duplicate entry when reused; the old reservation plus the new one otherwise.
        assert [r.connection_id for r in store.get_all()] == ["agg-conn-001"] + ([] if reused else ["agg-conn-002"])
