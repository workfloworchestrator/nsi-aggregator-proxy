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


"""Tests for error event deduplication in _query_error_events."""

import httpx
import pytest
from fastapi.testclient import TestClient

from aggregator_proxy.main import app
from aggregator_proxy.models import P2PS, CriteriaResponse, ReservationStatus
from aggregator_proxy.nsi_soap import parse_correlation_id
from aggregator_proxy.reservation_store import Reservation, ReservationStore
from tests.conftest import (
    build_error_event_xml,
    build_query_notification_sync_response,
    build_query_summary_sync_response,
)

CONNECTION_ID = "conn-dedup-001"


def _make_reservation(
    connection_id: str = CONNECTION_ID,
    status: ReservationStatus = ReservationStatus.RESERVED,
) -> Reservation:
    return Reservation(
        connection_id=connection_id,
        status=status,
        global_reservation_id="urn:uuid:dedup-test",
        description="dedup test reservation",
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


def _nsi_handler_with_error_events(*error_event_xmls: str) -> object:
    """Return an NSI handler that returns error events from queryNotificationSync."""

    def handler(request: httpx.Request) -> httpx.Response:
        cid = parse_correlation_id(request.content)
        body = request.content.decode()
        if "queryNotificationSync" in body:
            return httpx.Response(200, content=build_query_notification_sync_response(cid, *error_event_xmls))
        return httpx.Response(
            200,
            content=build_query_summary_sync_response(
                connection_id=CONNECTION_ID,
                correlation_id=cid,
                provision_state="Released",
            ),
        )

    return handler


@pytest.fixture()
def store() -> ReservationStore:
    return ReservationStore()


@pytest.fixture()
def _app_state(store: ReservationStore) -> None:
    app.state.reservation_store = store
    app.state.callback_client = httpx.AsyncClient()


class TestErrorEventDeduplication:
    """Tests for error event deduplication in _query_error_events."""

    def test_seen_ids_tracked_on_reservation(self, _app_state: None, store: ReservationStore) -> None:
        """After querying, the reservation should track seen notification IDs."""
        store.create(_make_reservation())
        event_xml = build_error_event_xml(connection_id=CONNECTION_ID, notification_id=42)
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_nsi_handler_with_error_events(event_xml))
        )
        client = TestClient(app, raise_server_exceptions=False)

        client.get(f"/reservations/{CONNECTION_ID}")

        reservation = store.get(CONNECTION_ID)
        assert reservation is not None
        assert reservation.seen_error_notification_ids == {42}

    def test_seen_ids_accumulate_across_queries(self, _app_state: None, store: ReservationStore) -> None:
        """Seen notification IDs accumulate as new events are discovered."""
        store.create(_make_reservation())
        event1_xml = build_error_event_xml(connection_id=CONNECTION_ID, notification_id=1)
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_nsi_handler_with_error_events(event1_xml))
        )
        client = TestClient(app, raise_server_exceptions=False)

        # First call — sees event 1
        client.get(f"/reservations/{CONNECTION_ID}")
        reservation = store.get(CONNECTION_ID)
        assert reservation is not None
        assert reservation.seen_error_notification_ids == {1}

        # Second call with both event 1 and new event 2
        event2_xml = build_error_event_xml(connection_id=CONNECTION_ID, notification_id=2, event="forcedEnd")
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_nsi_handler_with_error_events(event1_xml, event2_xml))
        )

        client.get(f"/reservations/{CONNECTION_ID}")
        assert reservation.seen_error_notification_ids == {1, 2}

    def test_first_error_event_logs_info(self, _app_state: None, store: ReservationStore, capsys: object) -> None:
        """First time seeing an error event should log at info level."""
        store.create(_make_reservation())
        event_xml = build_error_event_xml(connection_id=CONNECTION_ID, notification_id=1)
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_nsi_handler_with_error_events(event_xml))
        )
        client = TestClient(app, raise_server_exceptions=False)

        client.get(f"/reservations/{CONNECTION_ID}")

        captured = capsys.readouterr()  # type: ignore[union-attr]
        assert "New error events detected" in captured.out

    def test_repeated_error_event_logs_already_seen(
        self, _app_state: None, store: ReservationStore, capsys: object
    ) -> None:
        """Second time seeing the same error event should log 'already seen' at debug."""
        store.create(_make_reservation())
        event_xml = build_error_event_xml(connection_id=CONNECTION_ID, notification_id=1)
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_nsi_handler_with_error_events(event_xml))
        )
        client = TestClient(app, raise_server_exceptions=False)

        # First call populates seen IDs
        client.get(f"/reservations/{CONNECTION_ID}")
        _ = capsys.readouterr()  # type: ignore[union-attr]

        # Second call — same event
        client.get(f"/reservations/{CONNECTION_ID}")
        captured = capsys.readouterr()  # type: ignore[union-attr]

        assert "New error events detected" not in captured.out
        assert "already seen" in captured.out

    def test_new_event_among_seen_events_logs_info(
        self, _app_state: None, store: ReservationStore, capsys: object
    ) -> None:
        """When a new event appears alongside already-seen events, log info for the new one."""
        store.create(_make_reservation())
        event1_xml = build_error_event_xml(connection_id=CONNECTION_ID, notification_id=1, event="deactivateFailed")
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_nsi_handler_with_error_events(event1_xml))
        )
        client = TestClient(app, raise_server_exceptions=False)

        # First call — sees event 1
        client.get(f"/reservations/{CONNECTION_ID}")
        _ = capsys.readouterr()  # type: ignore[union-attr]

        # Second call — event 1 + new event 2
        event2_xml = build_error_event_xml(connection_id=CONNECTION_ID, notification_id=2, event="forcedEnd")
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_nsi_handler_with_error_events(event1_xml, event2_xml))
        )

        client.get(f"/reservations/{CONNECTION_ID}")
        captured = capsys.readouterr()  # type: ignore[union-attr]
        assert "New error events detected" in captured.out

    def test_no_error_events_does_not_set_seen_ids(self, _app_state: None, store: ReservationStore) -> None:
        """When no error events are returned, seen_error_notification_ids stays None."""
        store.create(_make_reservation())
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_nsi_handler_with_error_events())
        )
        client = TestClient(app, raise_server_exceptions=False)

        client.get(f"/reservations/{CONNECTION_ID}")

        reservation = store.get(CONNECTION_ID)
        assert reservation is not None
        assert reservation.seen_error_notification_ids is None


class TestErrorEventDeduplicationRefreshAll:
    """Tests for error event dedup via GET /reservations (refresh all / startup scenario).

    Unlike GET /reservations/{connectionId}, the list endpoint calls _refresh_all_reservations
    which may create new reservations that don't yet exist in the store.
    """

    def test_refresh_all_tracks_seen_ids_for_new_reservation(
        self, _app_state: None, store: ReservationStore
    ) -> None:
        """Error events discovered during refresh-all are tracked even for new reservations."""
        event_xml = build_error_event_xml(connection_id=CONNECTION_ID, notification_id=10)
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_nsi_handler_with_error_events(event_xml))
        )
        client = TestClient(app, raise_server_exceptions=False)

        # Store is empty — reservation is created by _refresh_all_reservations
        resp = client.get("/reservations")
        assert resp.status_code == 200

        reservation = store.get(CONNECTION_ID)
        assert reservation is not None
        assert reservation.seen_error_notification_ids == {10}

    def test_refresh_all_dedup_across_repeated_calls(
        self, _app_state: None, store: ReservationStore, capsys: object
    ) -> None:
        """Second GET /reservations with same error events should log 'already seen'."""
        event_xml = build_error_event_xml(connection_id=CONNECTION_ID, notification_id=5)
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_nsi_handler_with_error_events(event_xml))
        )
        client = TestClient(app, raise_server_exceptions=False)

        # First call — creates reservation and sees event
        client.get("/reservations")
        captured = capsys.readouterr()  # type: ignore[union-attr]
        assert "New error events detected" in captured.out

        # Second call — same event should be deduplicated
        client.get("/reservations")
        captured = capsys.readouterr()  # type: ignore[union-attr]
        assert "New error events detected" not in captured.out
        assert "already seen" in captured.out

    def test_refresh_all_new_event_after_initial_logs_info(
        self, _app_state: None, store: ReservationStore, capsys: object
    ) -> None:
        """A new event appearing on a subsequent refresh-all should log at info."""
        event1_xml = build_error_event_xml(connection_id=CONNECTION_ID, notification_id=1, event="deactivateFailed")
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_nsi_handler_with_error_events(event1_xml))
        )
        client = TestClient(app, raise_server_exceptions=False)

        # First call — sees event 1
        client.get("/reservations")
        _ = capsys.readouterr()  # type: ignore[union-attr]

        # Second call — event 1 + new event 2
        event2_xml = build_error_event_xml(connection_id=CONNECTION_ID, notification_id=2, event="forcedEnd")
        app.state.nsi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_nsi_handler_with_error_events(event1_xml, event2_xml))
        )

        client.get("/reservations")
        captured = capsys.readouterr()  # type: ignore[union-attr]
        assert "New error events detected" in captured.out
