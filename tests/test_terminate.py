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


"""Tests for DELETE /reservations/{connectionId} (terminate)."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from aggregator_proxy.main import app
from aggregator_proxy.models import ReservationStatus
from aggregator_proxy.nsi_soap import build_soap_fault, parse_correlation_id
from aggregator_proxy.reservation_store import Operation, Reservation, ReservationStore
from tests.conftest import (
    build_acknowledgment_xml,
    build_empty_query_summary_sync_response,
    build_message_delivery_timeout_xml,
    build_query_notification_sync_response,
    build_query_summary_sync_response,
    build_soap_envelope,
    get_pending_correlation_id,
    make_reservation,
    utc_now_iso,
    wait_until,
)

CALLBACK_URL = "http://callback.example.com/result"
CONNECTION_ID = "test-conn-789"


def _terminate_confirmed_xml(correlation_id: str) -> bytes:
    return build_soap_envelope(
        f"<terminateConfirmed><connectionId>{CONNECTION_ID}</connectionId></terminateConfirmed>",
        correlation_id=correlation_id,
    )


def _make_reservation(status: ReservationStatus = ReservationStatus.RESERVED) -> Reservation:
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
    provision_state: str = "Released",
    data_plane_active: bool = False,
    lifecycle_state: str = "Created",
    reservation_state: str = "ReserveStart",
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
                        lifecycle_state=lifecycle_state,
                        reservation_state=reservation_state,
                    ),
                )
            return httpx.Response(200, content=build_empty_query_summary_sync_response(cid))
        return httpx.Response(200, content=build_acknowledgment_xml(cid))

    return handler


def _nsi_handler(request: httpx.Request) -> httpx.Response:
    return _make_nsi_handler()(request)


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

    @pytest.mark.parametrize(
        ("status", "handler_states", "reserve_in_flight"),
        [
            pytest.param(
                ReservationStatus.ACTIVATED,
                {"provision_state": "Provisioned", "data_plane_active": True},
                False,
                id="activated",
            ),
            pytest.param(
                ReservationStatus.RESERVING,
                {"reservation_state": "ReserveChecking"},
                True,
                id="reserving-with-our-reserve-in-flight",
            ),
            pytest.param(
                ReservationStatus.RESERVING,
                {"reservation_state": "ReserveChecking", "lifecycle_state": "Terminating"},
                True,
                id="terminating-with-our-reserve-in-flight",
            ),
            # FAILED from the aggregator's history while our reserve task still waits on its callback.
            pytest.param(
                ReservationStatus.FAILED,
                {"reservation_state": "ReserveFailed"},
                True,
                id="failed-with-our-reserve-in-flight",
            ),
        ],
    )
    def test_terminate_returns_409(
        self,
        store: ReservationStore,
        status: ReservationStatus,
        handler_states: dict[str, object],
        reserve_in_flight: bool,
    ) -> None:
        store.create(_make_reservation(status=status))
        if reserve_in_flight:
            store.task_started(Operation.RESERVE, CONNECTION_ID)
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_make_nsi_handler(**handler_states))  # type: ignore[arg-type]
        )
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.request(
            "DELETE",
            f"/reservations/{CONNECTION_ID}",
            json={"callbackURL": "http://callback.example.com/terminate"},
        )
        assert resp.status_code == 409
        # A refused terminate must not redirect a running operation's result to its own callback.
        assert store.get(CONNECTION_ID).callback_url == CALLBACK_URL  # type: ignore[union-attr]


class TestTerminateIdempotency:
    """A retried terminate on an already-terminated connection re-delivers instead of re-dispatching."""

    @pytest.mark.anyio()
    @pytest.mark.parametrize(
        "reserve_in_flight",
        [
            pytest.param(False, id="nothing-running"),
            # Terminated elsewhere while our reserve still waits: re-deliver all the same.
            pytest.param(True, id="reserve-in-flight"),
        ],
    )
    async def test_terminate_already_terminated_redelivers_callback(
        self, store: ReservationStore, reserve_in_flight: bool
    ) -> None:
        store.create(_make_reservation(status=ReservationStatus.TERMINATED))
        if reserve_in_flight:
            store.task_started(Operation.RESERVE, CONNECTION_ID)
        new_callback = "http://callback.example.com/retry"
        delivered: list[str] = []

        def callback_handler(request: httpx.Request) -> httpx.Response:
            delivered.append(str(request.url))
            return httpx.Response(200)

        # Terminated lifecycle → TERMINATED: re-deliver the result to the new url, do not re-terminate.
        handler = _make_nsi_handler(lifecycle_state="Terminated")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.request(
                        "DELETE", f"/reservations/{CONNECTION_ID}", json={"callbackURL": new_callback}
                    )
                    await asyncio.sleep(0.1)  # let the background re-delivery run while cb_client is open
        assert resp.status_code == 202
        assert delivered == [new_callback]
        assert len(store._pending) == 0  # noqa: SLF001 — no terminate was dispatched
        reservation = store.get(CONNECTION_ID)
        assert reservation is not None
        assert reservation.callback_url == CALLBACK_URL  # a running operation keeps delivering to its own


class TestTerminateAggregatorFailure:
    """Test when the aggregator is unreachable or returns errors."""

    def test_aggregator_unreachable_returns_502(self, store: ReservationStore) -> None:
        """ConnectError when sending the terminate request → 502."""
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
                    ),
                )
            # terminate call → fail
            call_count += 1
            raise httpx.ConnectError("connection refused")

        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.state.callback_client = httpx.AsyncClient()
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.request("DELETE", f"/reservations/{CONNECTION_ID}", json={"callbackURL": CALLBACK_URL})
        assert resp.status_code == 502
        assert call_count == 1

    def test_unexpected_sync_response_returns_502(self, store: ReservationStore) -> None:
        """Aggregator returns a non-Acknowledgment for terminate → 502."""
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
                    ),
                )
            body = "<reserveResponse><connectionId>wrong</connectionId></reserveResponse>"
            return httpx.Response(200, content=build_soap_envelope(body, cid))

        app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.state.callback_client = httpx.AsyncClient()
        app.state.reservation_store = store
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.request("DELETE", f"/reservations/{CONNECTION_ID}", json={"callbackURL": CALLBACK_URL})
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

        resp = client.request("DELETE", f"/reservations/{CONNECTION_ID}", json={"callbackURL": CALLBACK_URL})
        assert resp.status_code == 502


class TestTerminateSucceeds:
    """Test successful terminate → TERMINATED from every state that allows it."""

    @pytest.mark.anyio()
    @pytest.mark.parametrize(
        ("status", "handler_states"),
        [
            pytest.param(ReservationStatus.RESERVED, {"reservation_state": "ReserveStart"}, id="reserved"),
            pytest.param(ReservationStatus.FAILED, {"reservation_state": "ReserveTimeout"}, id="failed"),
            # A child never answered: our reserve reported FAILED, the aggregator still reserves.
            pytest.param(
                ReservationStatus.FAILED, {"reservation_state": "ReserveChecking"}, id="reserving-without-our-reserve"
            ),
            # An earlier terminate is stuck at the aggregator while the data plane is still up.
            pytest.param(
                ReservationStatus.ACTIVATED,
                {"lifecycle_state": "Terminating", "provision_state": "Provisioned", "data_plane_active": True},
                id="retry-while-terminating",
            ),
        ],
    )
    async def test_terminate(
        self, store: ReservationStore, status: ReservationStatus, handler_states: dict[str, object]
    ) -> None:
        store.create(_make_reservation(status=status))

        handler = _make_nsi_handler(**handler_states)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as nsi_client:
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

                    cid = get_pending_correlation_id(store)

                    # Simulate terminateConfirmed callback
                    await test_client.post("/nsi/v2/callback", content=_terminate_confirmed_xml(cid))
                    await asyncio.sleep(0.1)

                    assert store.get(CONNECTION_ID).status == ReservationStatus.TERMINATED  # type: ignore[union-attr]


@dataclass
class _Aggregator:
    """Reports ``lifecycle_state`` to every query and acknowledges every terminate, counting them.

    ``query_failure`` makes every query after the first terminate fail: "unavailable" with a 503,
    "malformed" with a body that does not parse. ``terminated_before_resend`` terminates the
    connection before a second terminate arrives, which is then refused with a fault.
    """

    lifecycle_state: str = "Created"
    terminates: int = 0
    query_failure: str | None = None
    terminated_before_resend: bool = False
    notifications: list[str] = field(default_factory=list)

    def delivery_timeout(self) -> bytes:
        """Raise a messageDeliveryTimeout: listed by queryNotificationSync, sent with its own correlationId."""
        notification = build_message_delivery_timeout_xml(CONNECTION_ID, len(self.notifications) + 1, utc_now_iso())
        self.notifications.append(notification)
        return build_soap_envelope(notification, f"urn:uuid:{uuid4()}")

    def handler(self, request: httpx.Request) -> httpx.Response:
        cid = parse_correlation_id(request.content)
        body = request.content.decode()
        if "query" in body and self.terminates > 0:
            match self.query_failure:
                case "unavailable":
                    return httpx.Response(503)
                case "malformed":
                    return httpx.Response(200, content=b"<not-soap")
        if "queryNotificationSync" in body:
            return httpx.Response(200, content=build_query_notification_sync_response(cid, *self.notifications))
        if "querySummarySync" in body:
            return httpx.Response(
                200,
                content=build_query_summary_sync_response(
                    connection_id=CONNECTION_ID, correlation_id=cid, lifecycle_state=self.lifecycle_state
                ),
            )
        self.terminates += 1
        if self.terminated_before_resend and self.terminates > 1:
            self.lifecycle_state = "Terminated"
            return httpx.Response(500, content=build_soap_fault("00201: INVALID_TRANSITION"))
        return httpx.Response(200, content=build_acknowledgment_xml(cid))


def _latest_pending_correlation_id(store: ReservationStore) -> str:
    """The most recently registered correlationId; a timed-out terminate's stays registered."""
    return list(store._pending)[-1]  # noqa: SLF001


class TestTerminateNotConfirmed:
    """An unconfirmed terminate is sent once more, and the result is the aggregator's state, never assumed."""

    @pytest.mark.anyio()
    @pytest.mark.parametrize(
        ("replies", "lifecycle_after_first", "expected_status", "expected_error"),
        [
            pytest.param(
                ["delivery-timeout", "confirmed"],
                "Terminating",
                ReservationStatus.TERMINATED,
                None,
                id="delivery-timeout-then-confirmed",
            ),
            pytest.param(
                [None, "confirmed"], "Terminating", ReservationStatus.TERMINATED, None, id="silence-then-confirmed"
            ),
            pytest.param(
                ["delivery-timeout", "delivery-timeout"],
                "Terminating",
                ReservationStatus.TERMINATING,
                "messageDeliveryTimeout: a child NSA did not reply",
                id="delivery-timeout-twice",
            ),
            pytest.param(
                [None, None],
                "Terminating",
                ReservationStatus.TERMINATING,
                "no terminateConfirmed received within timeout",
                id="silence-twice",
            ),
            pytest.param(
                ["delivery-timeout"],
                "Terminated",
                ReservationStatus.TERMINATED,
                None,
                id="aggregator-terminated-meanwhile",
            ),
        ],
    )
    async def test_terminate_not_confirmed(
        self,
        store: ReservationStore,
        monkeypatch: pytest.MonkeyPatch,
        replies: list[str | None],
        lifecycle_after_first: str,
        expected_status: ReservationStatus,
        expected_error: str | None,
    ) -> None:
        """Each reply answers one terminate, so as many terminates are sent as there are replies."""
        from aggregator_proxy import settings as settings_module

        monkeypatch.setattr(settings_module.settings, "nsi_timeout", 0.2)
        store.create(_make_reservation(status=ReservationStatus.RESERVED))
        aggregator = _Aggregator()
        callbacks: list[dict[str, object]] = []

        def callback_handler(request: httpx.Request) -> httpx.Response:
            callbacks.append(json.loads(request.content))
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(aggregator.handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.request(
                        "DELETE", f"/reservations/{CONNECTION_ID}", json={"callbackURL": CALLBACK_URL}
                    )
                    assert resp.status_code == 202
                    aggregator.lifecycle_state = lifecycle_after_first

                    for sent, reply in enumerate(replies, start=1):
                        await wait_until(lambda sent=sent: aggregator.terminates == sent)  # type: ignore[misc]
                        match reply:
                            case "confirmed":
                                aggregator.lifecycle_state = "Terminated"
                                cid = _latest_pending_correlation_id(store)
                                await client.post("/nsi/v2/callback", content=_terminate_confirmed_xml(cid))
                            case "delivery-timeout":
                                ack = await client.post("/nsi/v2/callback", content=aggregator.delivery_timeout())
                                assert ack.status_code == 200

                    await wait_until(lambda: bool(callbacks))
                    # GET is how a caller without a callbackURL reads the result, reason included.
                    listed = (await client.get(f"/reservations/{CONNECTION_ID}")).json()

        assert aggregator.terminates == len(replies)
        assert callbacks[0]["status"] == expected_status
        reservation = store.get(CONNECTION_ID)
        assert reservation is not None
        assert reservation.status == expected_status
        assert (reservation.last_error or "").startswith(expected_error or "")
        assert (reservation.last_error is None) == (expected_error is None)
        assert (listed["status"], listed["lastError"]) == (expected_status, reservation.last_error)

    @pytest.mark.anyio()
    @pytest.mark.parametrize(
        "aggregator",
        [
            # The refresh deciding whether to resend fails: resend anyway, it is harmless.
            pytest.param(_Aggregator(query_failure="unavailable"), id="refresh-unavailable"),
            pytest.param(_Aggregator(query_failure="malformed"), id="refresh-malformed"),
            # The aggregator terminated before the resend arrived, and refuses it.
            pytest.param(_Aggregator(terminated_before_resend=True), id="resend-refused-after-terminated"),
        ],
    )
    async def test_terminate_settles_when_the_aggregator_misbehaves(
        self, store: ReservationStore, monkeypatch: pytest.MonkeyPatch, aggregator: _Aggregator
    ) -> None:
        from aggregator_proxy import settings as settings_module

        monkeypatch.setattr(settings_module.settings, "nsi_timeout", 0.2)
        store.create(_make_reservation(status=ReservationStatus.RESERVED))
        callbacks: list[dict[str, object]] = []

        def callback_handler(request: httpx.Request) -> httpx.Response:
            callbacks.append(json.loads(request.content))
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(aggregator.handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.request(
                        "DELETE", f"/reservations/{CONNECTION_ID}", json={"callbackURL": CALLBACK_URL}
                    )
                    assert resp.status_code == 202
                    aggregator.lifecycle_state = "Terminating"
                    await client.post("/nsi/v2/callback", content=aggregator.delivery_timeout())
                    if not aggregator.terminated_before_resend:
                        await wait_until(lambda: aggregator.terminates == 2)
                        cid = _latest_pending_correlation_id(store)
                        await client.post("/nsi/v2/callback", content=_terminate_confirmed_xml(cid))
                    await wait_until(lambda: bool(callbacks))

        assert aggregator.terminates == 2
        assert [callback["status"] for callback in callbacks] == [ReservationStatus.TERMINATED]


class TestTerminateWhileTerminating:
    """A second terminate while one of ours runs is not sent; the result goes to every callbackURL given."""

    @pytest.mark.anyio()
    @pytest.mark.parametrize(
        ("second_body", "expected_urls"),
        [
            pytest.param(None, [CALLBACK_URL], id="without-callback"),
            pytest.param({"callbackURL": CALLBACK_URL}, [CALLBACK_URL], id="same-callback"),
            pytest.param(
                {"callbackURL": "http://callback.example.com/retry"},
                [CALLBACK_URL, "http://callback.example.com/retry"],
                id="new-callback-both-delivered",
            ),
        ],
    )
    async def test_second_terminate(
        self, store: ReservationStore, second_body: dict[str, str] | None, expected_urls: list[str]
    ) -> None:
        store.create(_make_reservation(status=ReservationStatus.RESERVED))
        aggregator = _Aggregator()
        delivered: list[str] = []

        def callback_handler(request: httpx.Request) -> httpx.Response:
            delivered.append(str(request.url))
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(aggregator.handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    first = await client.request(
                        "DELETE", f"/reservations/{CONNECTION_ID}", json={"callbackURL": CALLBACK_URL}
                    )
                    cid = get_pending_correlation_id(store)
                    second = await client.request("DELETE", f"/reservations/{CONNECTION_ID}", json=second_body)
                    await client.post("/nsi/v2/callback", content=_terminate_confirmed_xml(cid))
                    await wait_until(lambda: len(delivered) == len(expected_urls))
                    await asyncio.sleep(0.05)

        assert (first.status_code, second.status_code) == (202, 202)
        assert aggregator.terminates == 1
        assert sorted(delivered) == sorted(expected_urls)
        assert not store.task_in_flight(Operation.TERMINATE, CONNECTION_ID)

    @pytest.mark.anyio()
    async def test_terminate_without_callback_url_delivers_nothing(self, store: ReservationStore) -> None:
        store.create(_make_reservation(status=ReservationStatus.RESERVED))
        aggregator = _Aggregator()
        callbacks: list[httpx.Request] = []

        def callback_handler(request: httpx.Request) -> httpx.Response:
            callbacks.append(request)
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(aggregator.handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(callback_handler)) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.request("DELETE", f"/reservations/{CONNECTION_ID}")
                    assert resp.status_code == 202
                    cid = get_pending_correlation_id(store)
                    await client.post("/nsi/v2/callback", content=_terminate_confirmed_xml(cid))
                    await wait_until(
                        lambda: store.get(CONNECTION_ID).status == ReservationStatus.TERMINATED  # type: ignore[union-attr]
                    )
                    await asyncio.sleep(0.05)

        assert callbacks == []
