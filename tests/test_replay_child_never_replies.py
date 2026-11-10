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


"""Replay what Safnari sends a requester when a child acknowledges requests but never replies.

Safnari then sends only reserveTimeout and messageDeliveryTimeout, in either order and with its own
correlationIds, and keeps the reservation pending. The fixture holds such sequences in Safnari's
exact message format, with synthetic ids; timestamps are set when a message is delivered, as Safnari
stamps them when it raises them. Every message must be acknowledged, the reserve must fail with the
latest as its reason, and a terminate that is never confirmed must end TERMINATING, never TERMINATED.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from lxml import etree

from aggregator_proxy.main import app
from aggregator_proxy.models import ReservationStatus
from aggregator_proxy.nsi_soap import parse_correlation_id
from aggregator_proxy.nsi_soap.namespaces import NSMAP
from aggregator_proxy.reservation_store import ReservationStore
from aggregator_proxy.settings import settings
from tests.conftest import (
    build_acknowledgment_xml,
    build_query_notification_sync_response,
    build_query_summary_sync_response,
    build_reserve_request_body,
    build_soap_envelope,
    utc_now_iso,
    wait_until,
)

_SEQUENCES: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "safnari_child_never_replies.json").read_text()
)
CALLBACK_URL = "http://callback.example.com/result"


def _sequence_id(index: int, sequence: dict[str, Any]) -> str:
    """Name a sequence by what happens after the reserve, e.g. "3-reserveTimeout-messageDeliveryTimeout"."""
    return "-".join([str(index), *(event.get("sent") or event["received"] for event in sequence["events"][1:])])


def _notification_xml(envelope: str) -> str:
    """The notification element of a callback, as queryNotificationSync lists it."""
    root = etree.fromstring(envelope.encode())
    body = root.find(f"{{{NSMAP['soapenv']}}}Body")
    assert body is not None
    return etree.tostring(body[0]).decode()


@dataclass
class _Safnari:
    """Answers as Safnari did: the reserve stays pending, the lifecycle follows our terminate."""

    connection_id: str
    lifecycle_state: str = "Created"
    notifications: list[str] = field(default_factory=list)
    terminates: int = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        cid = parse_correlation_id(request.content)
        match request.headers["SOAPAction"].strip('"').rsplit("/", 1)[-1]:
            case "queryNotificationSync":
                return httpx.Response(200, content=build_query_notification_sync_response(cid, *self.notifications))
            case "querySummarySync":
                return httpx.Response(
                    200,
                    content=build_query_summary_sync_response(
                        connection_id=self.connection_id,
                        correlation_id=cid,
                        reservation_state="ReserveChecking",
                        lifecycle_state=self.lifecycle_state,
                    ),
                )
            case "reserve":
                return httpx.Response(
                    200,
                    content=build_soap_envelope(
                        f"<reserveResponse><connectionId>{self.connection_id}</connectionId></reserveResponse>", cid
                    ),
                )
            case "terminate":
                self.terminates += 1
                self.lifecycle_state = "Terminating"
                return httpx.Response(200, content=build_acknowledgment_xml(cid))
            case action:
                raise AssertionError(f"unexpected request to the aggregator: {action}")


def _phases(events: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]] | None]:
    """Split a sequence into the callbacks after the reserve and, if terminated, those after the terminate."""
    sent = [index for index, event in enumerate(events) if event.get("sent") == "terminate"]
    if not sent:
        return events[1:], None
    return events[1 : sent[0]], events[sent[0] + 1 :]


def _latest(events: list[dict[str, str]]) -> str:
    """The operation of the notification Safnari raised last, by its notificationId."""
    return max(events, key=lambda event: int(re.findall(r"<notificationId>(\d+)<", event["xml"])[0]))["received"]


@pytest.mark.anyio()
@pytest.mark.parametrize("sequence", _SEQUENCES, ids=[_sequence_id(i, s) for i, s in enumerate(_SEQUENCES)])
async def test_replay(store: ReservationStore, monkeypatch: pytest.MonkeyPatch, sequence: dict[str, Any]) -> None:
    monkeypatch.setattr(settings, "nsi_timeout", 0.3)
    connection_id = sequence["connectionId"]
    after_reserve, after_terminate = _phases(sequence["events"])
    safnari = _Safnari(connection_id)
    callbacks: list[dict[str, Any]] = []

    def callback_handler(request: httpx.Request) -> httpx.Response:
        callbacks.append(json.loads(request.content))
        return httpx.Response(200)

    async def deliver(client: httpx.AsyncClient, events: list[dict[str, str]]) -> None:
        for event in events:
            envelope = re.sub(r"<timeStamp>[^<]*</timeStamp>", f"<timeStamp>{utc_now_iso()}</timeStamp>", event["xml"])
            safnari.notifications.append(_notification_xml(envelope))
            resp = await client.post("/nsi/v2/callback", content=envelope.encode())
            assert resp.status_code == 200, event["received"]

    async with httpx.AsyncClient(transport=httpx.MockTransport(safnari.handler)) as nsi_client:
        async with httpx.AsyncClient(transport=httpx.MockTransport(callback_handler)) as cb_client:
            app.state.nsi_client = nsi_client
            app.state.callback_client = cb_client
            app.state.reservation_store = store
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/reservations", json=build_reserve_request_body(CALLBACK_URL, settings.provider_nsa)
                )
                assert resp.status_code == 202
                await deliver(client, after_reserve)
                assert callbacks == [], "a timeout notification ended the reserve's wait"
                await wait_until(lambda: len(callbacks) == 1)

                assert callbacks[0]["status"] == ReservationStatus.FAILED
                assert callbacks[0]["lastError"].startswith(_latest(after_reserve))
                listed = (await client.get(f"/reservations/{connection_id}")).json()
                assert listed["status"] == ReservationStatus.FAILED

                if after_terminate is not None:
                    resp = await client.request(
                        "DELETE", f"/reservations/{connection_id}", json={"callbackURL": CALLBACK_URL}
                    )
                    assert resp.status_code == 202
                    await wait_until(lambda: safnari.terminates == 1)
                    await deliver(client, after_terminate)
                    await wait_until(lambda: len(callbacks) == 2)

                    assert safnari.terminates == 2, "an unconfirmed terminate is sent once more"
                    assert callbacks[1]["status"] == ReservationStatus.TERMINATING
                    assert callbacks[1]["lastError"].startswith("messageDeliveryTimeout")
                    listed = (await client.get(f"/reservations/{connection_id}")).json()
                    assert listed["status"] == ReservationStatus.TERMINATING

    assert ReservationStatus.TERMINATED not in [callback["status"] for callback in callbacks]
