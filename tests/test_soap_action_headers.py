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


"""Tests that verify the correct SOAPAction header is sent for each NSI operation."""

import asyncio
from collections.abc import Callable

import httpx
import pytest

from aggregator_proxy.main import app
from aggregator_proxy.models import P2PS, CriteriaResponse, ReservationStatus
from aggregator_proxy.nsi_soap import parse_correlation_id
from aggregator_proxy.nsi_soap.namespaces import NSMAP
from aggregator_proxy.reservation_store import Reservation, ReservationStore
from tests.conftest import (
    build_empty_query_summary_sync_response,
    build_query_notification_sync_response,
    build_query_recursive_confirmed_response,
    build_query_summary_sync_response,
)

_C = NSMAP["nsi_ctypes"]
_H = NSMAP["nsi_headers"]
_S = NSMAP["soapenv"]
_P = NSMAP["nsi_p2p"]

_SOAP_ACTION_BASE = "http://schemas.ogf.org/nsi/2013/12/connection/service"

CALLBACK_URL = "http://callback.example.com/result"
CONNECTION_ID = "test-conn-soap-action"
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


def _acknowledgment_xml(correlation_id: str) -> bytes:
    return _make_soap("<acknowledgment/>", correlation_id)


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
        requester_nsa=REQUESTER_NSA,
        provider_nsa=PROVIDER_NSA,
        callback_url=CALLBACK_URL,
    )


RequestRecorder = Callable[[httpx.Request], None]


async def _wait_for_captured(
    captured: list[httpx.Request],
    predicate: Callable[[httpx.Request], bool],
    *,
    polls: int = 200,
    interval: float = 0.01,
) -> None:
    """Poll ``captured`` until at least one request matches ``predicate``."""
    for _ in range(polls):
        if any(predicate(r) for r in captured):
            return
        await asyncio.sleep(interval)


def _recording_handler(
    recorder: list[httpx.Request],
    response_factory: Callable[[httpx.Request], httpx.Response],
) -> Callable[[httpx.Request], httpx.Response]:
    """Wrap a response factory to also record every request."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return response_factory(request)

    return handler


class TestStartupQuerySoapAction:
    """Verify SOAPAction on the querySummarySync sent at startup."""

    @pytest.mark.anyio()
    async def test_startup_query_summary_sync_soap_action(self) -> None:
        captured: list[httpx.Request] = []

        def nsi_response(request: httpx.Request) -> httpx.Response:
            cid = parse_correlation_id(request.content)
            return httpx.Response(200, content=build_empty_query_summary_sync_response(cid))

        handler = _recording_handler(captured, nsi_response)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as nsi_client:
            app.state.nsi_client = nsi_client
            app.state.callback_client = httpx.AsyncClient()
            app.state.reservation_store = ReservationStore()

            from aggregator_proxy.routers.reservations import _refresh_all_reservations

            await _refresh_all_reservations(nsi_client, app.state.reservation_store)

        assert len(captured) == 1
        assert captured[0].headers["SOAPAction"] == f'"{_SOAP_ACTION_BASE}/querySummarySync"'


class TestReserveSoapAction:
    """Verify SOAPAction on the reserve request."""

    @pytest.mark.anyio()
    async def test_reserve_sends_correct_soap_action(self) -> None:
        captured: list[httpx.Request] = []

        def nsi_response(request: httpx.Request) -> httpx.Response:
            cid = parse_correlation_id(request.content)
            body = request.content.decode()
            if "queryNotificationSync" in body:
                return httpx.Response(200, content=build_query_notification_sync_response(cid))
            if "querySummarySync" in body:
                return httpx.Response(200, content=build_empty_query_summary_sync_response(cid))
            if "reserveCommit" in body:
                return httpx.Response(200, content=_acknowledgment_xml(cid))
            # reserve
            return httpx.Response(
                200,
                content=_make_soap(
                    f"<reserveResponse><connectionId>{CONNECTION_ID}</connectionId></reserveResponse>", cid
                ),
            )

        handler = _recording_handler(captured, nsi_response)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = ReservationStore()

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:
                    resp = await test_client.post(
                        "/reservations",
                        json={
                            "description": "test",
                            "criteria": {
                                "p2ps": {
                                    "capacity": 1000,
                                    "sourceSTP": "urn:ogf:network:example.net:2025:src?vlan=100",
                                    "destSTP": "urn:ogf:network:example.net:2025:dst?vlan=200",
                                },
                            },
                            "requesterNSA": REQUESTER_NSA,
                            "providerNSA": PROVIDER_NSA,
                            "callbackURL": CALLBACK_URL,
                        },
                    )
                    assert resp.status_code == 202
                    await asyncio.sleep(0.1)

        reserve_requests = [r for r in captured if b"<reserve>" in r.content or b":reserve>" in r.content]
        assert len(reserve_requests) >= 1
        assert reserve_requests[0].headers["SOAPAction"] == f'"{_SOAP_ACTION_BASE}/reserve"'


class TestProvisionSoapAction:
    """Verify SOAPAction on the provision request."""

    @pytest.mark.anyio()
    async def test_provision_sends_correct_soap_action(self) -> None:
        captured: list[httpx.Request] = []
        store = ReservationStore()
        store.create(_make_reservation())

        def nsi_response(request: httpx.Request) -> httpx.Response:
            cid = parse_correlation_id(request.content)
            body = request.content.decode()
            if "queryNotificationSync" in body:
                return httpx.Response(200, content=build_query_notification_sync_response(cid))
            if "querySummarySync" in body:
                return httpx.Response(
                    200,
                    content=build_query_summary_sync_response(
                        CONNECTION_ID, cid, reservation_state="ReserveStart", provision_state="Released"
                    ),
                )
            # provision
            return httpx.Response(200, content=_acknowledgment_xml(cid))

        handler = _recording_handler(captured, nsi_response)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as cb_client:
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
                    await asyncio.sleep(0.1)

        provision_requests = [r for r in captured if b"provision>" in r.content and b"querySummary" not in r.content]
        assert len(provision_requests) >= 1
        assert provision_requests[0].headers["SOAPAction"] == f'"{_SOAP_ACTION_BASE}/provision"'

        # Also verify querySummarySync and queryNotificationSync actions in the same flow
        qs_requests = [r for r in captured if b"querySummarySync" in r.content]
        assert all(r.headers["SOAPAction"] == f'"{_SOAP_ACTION_BASE}/querySummarySync"' for r in qs_requests)

        qn_requests = [r for r in captured if b"queryNotificationSync" in r.content]
        assert all(r.headers["SOAPAction"] == f'"{_SOAP_ACTION_BASE}/queryNotificationSync"' for r in qn_requests)


class TestReleaseSoapAction:
    """Verify SOAPAction on the release request."""

    @pytest.mark.anyio()
    async def test_release_sends_correct_soap_action(self) -> None:
        captured: list[httpx.Request] = []
        store = ReservationStore()
        store.create(_make_reservation(status=ReservationStatus.ACTIVATED))

        def nsi_response(request: httpx.Request) -> httpx.Response:
            cid = parse_correlation_id(request.content)
            body = request.content.decode()
            if "queryNotificationSync" in body:
                return httpx.Response(200, content=build_query_notification_sync_response(cid))
            if "querySummarySync" in body:
                return httpx.Response(
                    200,
                    content=build_query_summary_sync_response(
                        CONNECTION_ID, cid, provision_state="Provisioned", data_plane_active=True
                    ),
                )
            # release
            return httpx.Response(200, content=_acknowledgment_xml(cid))

        handler = _recording_handler(captured, nsi_response)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as cb_client:
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
                    await asyncio.sleep(0.1)

        release_requests = [r for r in captured if b"release>" in r.content and b"querySummary" not in r.content]
        assert len(release_requests) >= 1
        assert release_requests[0].headers["SOAPAction"] == f'"{_SOAP_ACTION_BASE}/release"'


class TestTerminateSoapAction:
    """Verify SOAPAction on the terminate request."""

    @pytest.mark.anyio()
    async def test_terminate_sends_correct_soap_action(self) -> None:
        captured: list[httpx.Request] = []
        store = ReservationStore()
        store.create(_make_reservation())

        def nsi_response(request: httpx.Request) -> httpx.Response:
            cid = parse_correlation_id(request.content)
            body = request.content.decode()
            if "queryNotificationSync" in body:
                return httpx.Response(200, content=build_query_notification_sync_response(cid))
            if "querySummarySync" in body:
                return httpx.Response(
                    200,
                    content=build_query_summary_sync_response(
                        CONNECTION_ID, cid, reservation_state="ReserveStart", provision_state="Released"
                    ),
                )
            # terminate
            return httpx.Response(200, content=_acknowledgment_xml(cid))

        handler = _recording_handler(captured, nsi_response)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as cb_client:
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
                    await asyncio.sleep(0.1)

        terminate_requests = [r for r in captured if b"terminate>" in r.content and b"querySummary" not in r.content]
        assert len(terminate_requests) >= 1
        assert terminate_requests[0].headers["SOAPAction"] == f'"{_SOAP_ACTION_BASE}/terminate"'


class TestQueryRecursiveSoapAction:
    """Verify SOAPAction on the queryRecursive request."""

    @pytest.mark.anyio()
    async def test_query_recursive_sends_correct_soap_action(self) -> None:
        captured: list[httpx.Request] = []
        store = ReservationStore()
        store.create(_make_reservation())

        def nsi_response(request: httpx.Request) -> httpx.Response:
            cid = parse_correlation_id(request.content)
            body = request.content.decode()
            if "queryNotificationSync" in body:
                return httpx.Response(200, content=build_query_notification_sync_response(cid))
            # queryRecursive — return acknowledgment
            return httpx.Response(200, content=_acknowledgment_xml(cid))

        handler = _recording_handler(captured, nsi_response)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as nsi_client:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as cb_client:
                app.state.nsi_client = nsi_client
                app.state.callback_client = cb_client
                app.state.reservation_store = store

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as test_client:

                    def _is_query_recursive(r: httpx.Request) -> bool:
                        return b"queryRecursive" in r.content and b"querySummary" not in r.content

                    async def deliver_callback() -> None:
                        await _wait_for_captured(captured, _is_query_recursive)
                        qr_req = next(filter(_is_query_recursive, captured))
                        cid = parse_correlation_id(qr_req.content)
                        callback_xml = build_query_recursive_confirmed_response(
                            connection_id=CONNECTION_ID, correlation_id=cid, children_xml=""
                        )
                        await test_client.post(
                            "/nsi/v2/callback",
                            content=callback_xml,
                            headers={"Content-Type": "text/xml"},
                        )

                    callback_task = asyncio.create_task(deliver_callback())
                    resp = await test_client.get(f"/reservations/{CONNECTION_ID}?detail=recursive")
                    await callback_task

                    assert resp.status_code == 200

        qr_requests = [r for r in captured if b"queryRecursive" in r.content and b"querySummary" not in r.content]
        assert len(qr_requests) >= 1
        assert qr_requests[0].headers["SOAPAction"] == f'"{_SOAP_ACTION_BASE}/queryRecursive"'
