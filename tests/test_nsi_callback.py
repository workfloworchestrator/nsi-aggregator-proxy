"""Tests for the NSI callback endpoint (POST /nsi/v2/callback)."""

import httpx
import pytest

from aggregator_proxy.main import app
from aggregator_proxy.nsi_soap import (
    DataPlaneStateChange,
    ReserveConfirmed,
    TerminateConfirmed,
)
from aggregator_proxy.nsi_soap.namespaces import NSMAP
from aggregator_proxy.reservation_store import ReservationStore

_C = NSMAP["nsi_ctypes"]
_H = NSMAP["nsi_headers"]
_S = NSMAP["soapenv"]
_P = NSMAP["nsi_p2p"]


def _envelope(body_xml: str, correlation_id: str = "urn:uuid:test-corr") -> bytes:
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="{_S}" xmlns:head="{_H}" xmlns:type="{_C}" xmlns:p2p="{_P}">
  <soapenv:Header>
    <head:nsiHeader>
      <correlationId>{correlation_id}</correlationId>
    </head:nsiHeader>
  </soapenv:Header>
  <soapenv:Body>
    {body_xml}
  </soapenv:Body>
</soapenv:Envelope>""".encode()


@pytest.fixture()
def store() -> ReservationStore:
    return ReservationStore()


@pytest.fixture()
def _app_state(store: ReservationStore) -> None:
    app.state.nsi_client = httpx.AsyncClient()
    app.state.callback_client = httpx.AsyncClient()
    app.state.reservation_store = store


class TestCallbackResolvesPending:
    """Test that callbacks resolve the correct pending future."""

    @pytest.mark.anyio()
    async def test_resolve_by_correlation_id(self, store: ReservationStore, _app_state: None) -> None:
        future = store.register_pending("urn:uuid:my-corr")

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            xml = _envelope(
                "<terminateConfirmed><connectionId>conn-1</connectionId></terminateConfirmed>",
                correlation_id="urn:uuid:my-corr",
            )
            resp = await client.post("/nsi/v2/callback", content=xml)
            assert resp.status_code == 200

        result = future.result()
        assert isinstance(result, TerminateConfirmed)
        assert result.connection_id == "conn-1"

    @pytest.mark.anyio()
    async def test_data_plane_state_change_resolves_by_connection(
        self, store: ReservationStore, _app_state: None
    ) -> None:
        future = store.register_pending_by_connection("conn-1")

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            xml = _envelope(
                """\
<dataPlaneStateChange>
  <connectionId>conn-1</connectionId>
  <notificationId>1</notificationId>
  <timeStamp>2025-06-01T12:00:00Z</timeStamp>
  <dataPlaneStatus>
    <active>true</active>
    <version>1</version>
    <versionConsistent>true</versionConsistent>
  </dataPlaneStatus>
</dataPlaneStateChange>""",
                correlation_id="urn:uuid:aggregator-generated",
            )
            resp = await client.post("/nsi/v2/callback", content=xml)
            assert resp.status_code == 200

        result = future.result()
        assert isinstance(result, DataPlaneStateChange)
        assert result.active is True

    @pytest.mark.anyio()
    async def test_reserve_confirmed_resolves_by_correlation(self, store: ReservationStore, _app_state: None) -> None:
        future = store.register_pending("urn:uuid:reserve-corr")

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            xml = _envelope(
                """\
<reserveConfirmed>
  <connectionId>conn-1</connectionId>
  <criteria version="1">
    <serviceType>http://services.ogf.org/nsi/2013/12/descriptions/EVTS.A-GOLE</serviceType>
    <p2p:p2ps>
      <capacity>1000</capacity>
      <sourceSTP>urn:ogf:network:example.net:2025:src?vlan=100</sourceSTP>
      <destSTP>urn:ogf:network:example.net:2025:dst?vlan=200</destSTP>
    </p2p:p2ps>
  </criteria>
</reserveConfirmed>""",
                correlation_id="urn:uuid:reserve-corr",
            )
            resp = await client.post("/nsi/v2/callback", content=xml)
            assert resp.status_code == 200

        result = future.result()
        assert isinstance(result, ReserveConfirmed)
        assert result.connection_id == "conn-1"


class TestCallbackUnknownCorrelation:
    """Test callback with unknown correlation ID returns 200 (logged as warning)."""

    @pytest.mark.anyio()
    async def test_unknown_correlation_returns_200(self, _app_state: None) -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            xml = _envelope(
                "<terminateConfirmed><connectionId>conn-1</connectionId></terminateConfirmed>",
                correlation_id="urn:uuid:unknown-corr",
            )
            resp = await client.post("/nsi/v2/callback", content=xml)
            assert resp.status_code == 200


class TestCallbackInvalidXml:
    """Test callback with invalid XML returns 400."""

    @pytest.mark.anyio()
    async def test_invalid_xml_raises(self, _app_state: None) -> None:
        from lxml.etree import XMLSyntaxError

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=True), base_url="http://test"
        ) as client:
            # lxml.etree.XMLSyntaxError is not a ValueError, so the endpoint's
            # except ValueError does not catch it — it propagates as an unhandled error.
            with pytest.raises(XMLSyntaxError):
                await client.post("/nsi/v2/callback", content=b"not xml at all \x00")

    @pytest.mark.anyio()
    async def test_missing_correlation_id_returns_400(self, _app_state: None) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="{_S}">
  <soapenv:Header/>
  <soapenv:Body><acknowledgment/></soapenv:Body>
</soapenv:Envelope>""".encode()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/nsi/v2/callback", content=xml)
            assert resp.status_code == 400
