"""Tests for the nsi_soap parse() dispatcher — all callback/response message types."""

import pytest

from aggregator_proxy.nsi_soap import (
    Acknowledgment,
    DataPlaneStateChange,
    ProvisionConfirmed,
    ReleaseConfirmed,
    ReserveCommitConfirmed,
    ReserveCommitFailed,
    ReserveConfirmed,
    ReserveFailed,
    ReserveResponse,
    ReserveTimeout,
    TerminateConfirmed,
    Variable,
    parse,
    parse_correlation_id,
)
from aggregator_proxy.nsi_soap.namespaces import NSMAP

_C = NSMAP["nsi_ctypes"]
_H = NSMAP["nsi_headers"]
_S = NSMAP["soapenv"]
_P = NSMAP["nsi_p2p"]


def _envelope(body_xml: str, correlation_id: str = "urn:uuid:test-corr-id") -> bytes:
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


class TestParseReserveResponse:
    def test_reserve_response(self) -> None:
        xml = _envelope("<reserveResponse><connectionId>conn-42</connectionId></reserveResponse>")
        msg = parse(xml)
        assert isinstance(msg, ReserveResponse)
        assert msg.connection_id == "conn-42"


class TestParseReserveConfirmed:
    def test_reserve_confirmed(self) -> None:
        xml = _envelope("""\
<reserveConfirmed>
  <connectionId>conn-42</connectionId>
  <criteria version="3">
    <serviceType>http://services.ogf.org/nsi/2013/12/descriptions/EVTS.A-GOLE</serviceType>
    <p2p:p2ps>
      <capacity>1000</capacity>
      <sourceSTP>urn:ogf:network:example.net:2025:src?vlan=100</sourceSTP>
      <destSTP>urn:ogf:network:example.net:2025:dst?vlan=200</destSTP>
    </p2p:p2ps>
  </criteria>
</reserveConfirmed>""")
        msg = parse(xml)
        assert isinstance(msg, ReserveConfirmed)
        assert msg.connection_id == "conn-42"
        assert msg.criteria_version == 3
        assert msg.service_type == "http://services.ogf.org/nsi/2013/12/descriptions/EVTS.A-GOLE"
        assert msg.capacity == 1000
        assert msg.source_stp == "urn:ogf:network:example.net:2025:src?vlan=100"
        assert msg.dest_stp == "urn:ogf:network:example.net:2025:dst?vlan=200"

    def test_reserve_confirmed_missing_criteria_raises(self) -> None:
        xml = _envelope("<reserveConfirmed><connectionId>conn-42</connectionId></reserveConfirmed>")
        with pytest.raises(ValueError, match="criteria"):
            parse(xml)

    def test_reserve_confirmed_missing_p2ps_raises(self) -> None:
        xml = _envelope("""\
<reserveConfirmed>
  <connectionId>conn-42</connectionId>
  <criteria version="1">
    <serviceType>some-type</serviceType>
  </criteria>
</reserveConfirmed>""")
        with pytest.raises(ValueError, match="p2ps"):
            parse(xml)


class TestParseReserveFailed:
    def test_reserve_failed_basic(self) -> None:
        xml = _envelope("""\
<reserveFailed>
  <connectionId>conn-42</connectionId>
  <serviceException>
    <nsaId>urn:ogf:network:example.net:2025:nsa</nsaId>
    <connectionId>conn-42</connectionId>
    <errorId>00700</errorId>
    <text>CAPACITY_UNAVAILABLE</text>
  </serviceException>
</reserveFailed>""")
        msg = parse(xml)
        assert isinstance(msg, ReserveFailed)
        assert msg.connection_id == "conn-42"
        assert msg.service_exception.nsa_id == "urn:ogf:network:example.net:2025:nsa"
        assert msg.service_exception.connection_id == "conn-42"
        assert msg.service_exception.error_id == "00700"
        assert msg.service_exception.text == "CAPACITY_UNAVAILABLE"
        assert msg.service_exception.child_exceptions is None
        assert msg.service_exception.variables is None

    def test_reserve_failed_with_child_exceptions(self) -> None:
        xml = _envelope("""\
<reserveFailed>
  <connectionId>conn-42</connectionId>
  <serviceException>
    <nsaId>urn:ogf:network:agg:2025:nsa</nsaId>
    <errorId>00700</errorId>
    <text>CAPACITY_UNAVAILABLE</text>
    <childException>
      <nsaId>urn:ogf:network:child:2025:nsa</nsaId>
      <connectionId>child-conn-1</connectionId>
      <errorId>00701</errorId>
      <text>No VLAN available on port</text>
    </childException>
  </serviceException>
</reserveFailed>""")
        msg = parse(xml)
        assert isinstance(msg, ReserveFailed)
        assert msg.service_exception.connection_id is None
        assert msg.service_exception.child_exceptions is not None
        assert len(msg.service_exception.child_exceptions) == 1
        child = msg.service_exception.child_exceptions[0]
        assert child.nsa_id == "urn:ogf:network:child:2025:nsa"
        assert child.connection_id == "child-conn-1"
        assert child.error_id == "00701"
        assert child.text == "No VLAN available on port"

    def test_reserve_failed_with_variables(self) -> None:
        xml = _envelope("""\
<reserveFailed>
  <connectionId>conn-42</connectionId>
  <serviceException>
    <nsaId>urn:ogf:network:agg:2025:nsa</nsaId>
    <connectionId>conn-42</connectionId>
    <errorId>00700</errorId>
    <text>CAPACITY_UNAVAILABLE</text>
    <variables>
      <variable type="capacity"><value>1000</value></variable>
      <variable type="available"><value>500</value></variable>
    </variables>
  </serviceException>
</reserveFailed>""")
        msg = parse(xml)
        assert isinstance(msg, ReserveFailed)
        assert msg.service_exception.variables is not None
        assert len(msg.service_exception.variables) == 2
        assert msg.service_exception.variables[0] == Variable(type="capacity", value="1000")
        assert msg.service_exception.variables[1] == Variable(type="available", value="500")

    def test_reserve_failed_missing_service_exception_raises(self) -> None:
        xml = _envelope("<reserveFailed><connectionId>conn-42</connectionId></reserveFailed>")
        with pytest.raises(ValueError, match="serviceException"):
            parse(xml)


class TestParseReserveTimeout:
    def test_reserve_timeout(self) -> None:
        xml = _envelope("""\
<reserveTimeout>
  <connectionId>conn-42</connectionId>
  <notificationId>5</notificationId>
  <timeStamp>2025-06-01T12:00:00Z</timeStamp>
  <timeoutValue>180</timeoutValue>
  <originatingConnectionId>orig-conn-1</originatingConnectionId>
  <originatingNSA>urn:ogf:network:child:2025:nsa</originatingNSA>
</reserveTimeout>""")
        msg = parse(xml)
        assert isinstance(msg, ReserveTimeout)
        assert msg.connection_id == "conn-42"
        assert msg.notification_id == 5
        assert msg.timestamp == "2025-06-01T12:00:00Z"
        assert msg.timeout_value == 180
        assert msg.originating_connection_id == "orig-conn-1"
        assert msg.originating_nsa == "urn:ogf:network:child:2025:nsa"


class TestParseReserveCommitFailed:
    def test_reserve_commit_failed(self) -> None:
        xml = _envelope("""\
<reserveCommitFailed>
  <connectionId>conn-42</connectionId>
  <serviceException>
    <nsaId>urn:ogf:network:example.net:2025:nsa</nsaId>
    <connectionId>conn-42</connectionId>
    <errorId>00500</errorId>
    <text>COMMIT_ERROR</text>
  </serviceException>
</reserveCommitFailed>""")
        msg = parse(xml)
        assert isinstance(msg, ReserveCommitFailed)
        assert msg.connection_id == "conn-42"
        assert msg.service_exception.error_id == "00500"

    def test_reserve_commit_failed_missing_exception_raises(self) -> None:
        xml = _envelope("<reserveCommitFailed><connectionId>conn-42</connectionId></reserveCommitFailed>")
        with pytest.raises(ValueError, match="serviceException"):
            parse(xml)


class TestParseReserveCommitConfirmed:
    def test_reserve_commit_confirmed(self) -> None:
        xml = _envelope("<reserveCommitConfirmed><connectionId>conn-42</connectionId></reserveCommitConfirmed>")
        msg = parse(xml)
        assert isinstance(msg, ReserveCommitConfirmed)
        assert msg.connection_id == "conn-42"


class TestParseProvisionConfirmed:
    def test_provision_confirmed(self) -> None:
        xml = _envelope("<provisionConfirmed><connectionId>conn-42</connectionId></provisionConfirmed>")
        msg = parse(xml)
        assert isinstance(msg, ProvisionConfirmed)
        assert msg.connection_id == "conn-42"


class TestParseDataPlaneStateChange:
    def test_active_true(self) -> None:
        xml = _envelope("""\
<dataPlaneStateChange>
  <connectionId>conn-42</connectionId>
  <notificationId>3</notificationId>
  <timeStamp>2025-06-01T14:00:00Z</timeStamp>
  <dataPlaneStatus>
    <active>true</active>
    <version>2</version>
    <versionConsistent>true</versionConsistent>
  </dataPlaneStatus>
</dataPlaneStateChange>""")
        msg = parse(xml)
        assert isinstance(msg, DataPlaneStateChange)
        assert msg.connection_id == "conn-42"
        assert msg.notification_id == 3
        assert msg.timestamp == "2025-06-01T14:00:00Z"
        assert msg.active is True
        assert msg.version == 2
        assert msg.version_consistent is True

    def test_active_false(self) -> None:
        xml = _envelope("""\
<dataPlaneStateChange>
  <connectionId>conn-42</connectionId>
  <notificationId>4</notificationId>
  <timeStamp>2025-06-01T15:00:00Z</timeStamp>
  <dataPlaneStatus>
    <active>false</active>
    <version>2</version>
    <versionConsistent>false</versionConsistent>
  </dataPlaneStatus>
</dataPlaneStateChange>""")
        msg = parse(xml)
        assert isinstance(msg, DataPlaneStateChange)
        assert msg.active is False
        assert msg.version_consistent is False

    def test_missing_data_plane_status_raises(self) -> None:
        xml = _envelope("""\
<dataPlaneStateChange>
  <connectionId>conn-42</connectionId>
  <notificationId>1</notificationId>
  <timeStamp>2025-06-01T12:00:00Z</timeStamp>
</dataPlaneStateChange>""")
        with pytest.raises(ValueError, match="dataPlaneStatus"):
            parse(xml)


class TestParseReleaseConfirmed:
    def test_release_confirmed(self) -> None:
        xml = _envelope("<releaseConfirmed><connectionId>conn-42</connectionId></releaseConfirmed>")
        msg = parse(xml)
        assert isinstance(msg, ReleaseConfirmed)
        assert msg.connection_id == "conn-42"


class TestParseTerminateConfirmed:
    def test_terminate_confirmed(self) -> None:
        xml = _envelope("<terminateConfirmed><connectionId>conn-42</connectionId></terminateConfirmed>")
        msg = parse(xml)
        assert isinstance(msg, TerminateConfirmed)
        assert msg.connection_id == "conn-42"


class TestParseAcknowledgment:
    def test_acknowledgment(self) -> None:
        xml = _envelope("<acknowledgment/>")
        msg = parse(xml)
        assert isinstance(msg, Acknowledgment)


class TestParseErrors:
    def test_empty_body_raises(self) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="{_S}">
  <soapenv:Body/>
</soapenv:Envelope>""".encode()
        with pytest.raises(ValueError, match="Body"):
            parse(xml)

    def test_missing_body_raises(self) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="{_S}">
  <soapenv:Header/>
</soapenv:Envelope>""".encode()
        with pytest.raises(ValueError, match="Body"):
            parse(xml)

    def test_unknown_operation_raises(self) -> None:
        xml = _envelope("<unknownOperation><connectionId>conn-42</connectionId></unknownOperation>")
        with pytest.raises(ValueError, match="Unknown NSI operation"):
            parse(xml)

    def test_malformed_xml_raises(self) -> None:
        from lxml.etree import XMLSyntaxError

        with pytest.raises(XMLSyntaxError):
            parse(b"<not valid xml")


class TestParseCorrelationId:
    def test_extracts_correlation_id(self) -> None:
        xml = _envelope("<acknowledgment/>", correlation_id="urn:uuid:abc-123")
        assert parse_correlation_id(xml) == "urn:uuid:abc-123"

    def test_missing_correlation_id_raises(self) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="{_S}">
  <soapenv:Header/>
  <soapenv:Body><acknowledgment/></soapenv:Body>
</soapenv:Envelope>""".encode()
        with pytest.raises(ValueError, match="correlationId"):
            parse_correlation_id(xml)
