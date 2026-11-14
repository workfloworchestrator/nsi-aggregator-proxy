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


"""An operation whose acknowledgement does not parse is a 502 and leaves nothing registered."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from aggregator_proxy.main import app
from aggregator_proxy.models import ReservationStatus
from aggregator_proxy.nsi_soap import parse_correlation_id
from aggregator_proxy.reservation_store import Operation, ReservationStore
from tests.conftest import (
    UNPARSEABLE,
    build_empty_query_summary_sync_response,
    build_query_notification_sync_response,
    build_query_summary_sync_response,
    build_reserve_request_body,
)

CONNECTION_ID = "test-conn-789"
CALLBACK = {"callbackURL": "http://callback.example.com/result"}


def _aggregator(states: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """Answer the queries with ``states``, and anything else, the operation under test, with garbage."""

    def handler(request: httpx.Request) -> httpx.Response:
        cid = parse_correlation_id(request.content)
        body = request.content.decode()
        if "queryNotificationSync" in body:
            return httpx.Response(200, content=build_query_notification_sync_response(cid))
        if "querySummarySync" in body:
            if CONNECTION_ID in body:
                content = build_query_summary_sync_response(connection_id=CONNECTION_ID, correlation_id=cid, **states)
                return httpx.Response(200, content=content)
            return httpx.Response(200, content=build_empty_query_summary_sync_response(cid))
        return httpx.Response(200, content=UNPARSEABLE)

    return handler


@pytest.mark.parametrize(
    ("method", "path", "body", "states", "stored"),
    [
        pytest.param("POST", "/reservations", build_reserve_request_body(), {}, [], id="reserve"),
        pytest.param(
            "POST",
            f"/reservations/{CONNECTION_ID}/provision",
            CALLBACK,
            {},
            [ReservationStatus.RESERVED],
            id="provision",
        ),
        pytest.param(
            "POST",
            f"/reservations/{CONNECTION_ID}/release",
            CALLBACK,
            {"provision_state": "Provisioned", "data_plane_active": True},
            [ReservationStatus.ACTIVATED],
            id="release",
        ),
        pytest.param("GET", f"/reservations/{CONNECTION_ID}?detail=recursive", None, {}, [], id="query-recursive"),
        pytest.param(
            "DELETE", f"/reservations/{CONNECTION_ID}", CALLBACK, {}, [ReservationStatus.RESERVED], id="terminate"
        ),
    ],
)
def test_unparseable_answer_is_a_502_and_leaves_nothing_registered(
    store: ReservationStore,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    states: dict[str, Any],
    stored: list[ReservationStatus],
) -> None:
    app.state.nsi_client = httpx.AsyncClient(transport=httpx.MockTransport(_aggregator(states)))
    app.state.callback_client = httpx.AsyncClient()
    app.state.reservation_store = store

    resp = TestClient(app, raise_server_exceptions=False).request(method, path, json=body)

    assert resp.status_code == 502
    assert not store._pending  # noqa: SLF001
    assert not store.task_in_flight(Operation.TERMINATE, CONNECTION_ID)
    assert [reservation.status for reservation in store.get_all()] == stored
