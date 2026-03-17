"""Tests for parse_query_summary_sync."""

import pytest

from aggregator_proxy.nsi_soap.parser import parse_query_summary_sync

_ENVELOPE_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
    ' xmlns:nsi_ctypes="http://schemas.ogf.org/nsi/2013/12/connection/types"'
    ' xmlns:nsi_p2p="http://schemas.ogf.org/nsi/2013/12/services/point2point">'
    "<soapenv:Body>"
    "<nsi_ctypes:querySummarySyncConfirmed>"
    "{reservations}"
    "</nsi_ctypes:querySummarySyncConfirmed>"
    "</soapenv:Body>"
    "</soapenv:Envelope>"
)

_RESERVATION_WITH_CRITERIA = (
    "<reservation>"
    "<connectionId>conn-001</connectionId>"
    "<globalReservationId>urn:uuid:550e8400-e29b-41d4-a716-446655440000</globalReservationId>"
    "<description>test reservation</description>"
    '<criteria version="2">'
    "<serviceType>http://services.ogf.org/nsi/2013/12/descriptions/EVTS.A-GOLE</serviceType>"
    "<nsi_p2p:p2ps>"
    "<capacity>1000</capacity>"
    "<sourceSTP>urn:ogf:network:example.net:2025:src?vlan=100</sourceSTP>"
    "<destSTP>urn:ogf:network:example.net:2025:dst?vlan=200</destSTP>"
    "</nsi_p2p:p2ps>"
    "</criteria>"
    "<requesterNSA>urn:ogf:network:example.net:2025:nsa:requester</requesterNSA>"
    "<connectionStates>"
    "<reservationState>ReserveStart</reservationState>"
    "<provisionState>Provisioned</provisionState>"
    "<lifecycleState>Created</lifecycleState>"
    "<dataPlaneStatus>"
    "<active>true</active>"
    "<version>1</version>"
    "<versionConsistent>true</versionConsistent>"
    "</dataPlaneStatus>"
    "</connectionStates>"
    "</reservation>"
)

_RESERVATION_WITHOUT_CRITERIA = (
    "<reservation>"
    "<connectionId>conn-002</connectionId>"
    "<globalReservationId>urn:uuid:aabbccdd-1122-3344-5566-778899001122</globalReservationId>"
    "<description>no criteria</description>"
    "<requesterNSA>urn:ogf:network:example.net:2025:nsa:requester</requesterNSA>"
    "<connectionStates>"
    "<reservationState>ReserveTimeout</reservationState>"
    "<provisionState>Released</provisionState>"
    "<lifecycleState>Created</lifecycleState>"
    "<dataPlaneStatus>"
    "<active>false</active>"
    "<version>0</version>"
    "<versionConsistent>false</versionConsistent>"
    "</dataPlaneStatus>"
    "</connectionStates>"
    "</reservation>"
)


def _build_xml(*reservation_fragments: str) -> bytes:
    return _ENVELOPE_TEMPLATE.format(reservations="".join(reservation_fragments)).encode()


class TestParseQuerySummarySync:
    """Tests for parse_query_summary_sync."""

    def test_empty_result(self) -> None:
        xml = _build_xml()
        result = parse_query_summary_sync(xml)
        assert result == []

    def test_single_reservation_with_criteria(self) -> None:
        xml = _build_xml(_RESERVATION_WITH_CRITERIA)
        result = parse_query_summary_sync(xml)
        assert len(result) == 1
        r = result[0]
        assert r.connection_id == "conn-001"
        assert r.global_reservation_id == "urn:uuid:550e8400-e29b-41d4-a716-446655440000"
        assert r.description == "test reservation"
        assert r.requester_nsa == "urn:ogf:network:example.net:2025:nsa:requester"
        assert r.criteria_version == 2
        assert r.service_type == "http://services.ogf.org/nsi/2013/12/descriptions/EVTS.A-GOLE"
        assert r.capacity == 1000
        assert r.source_stp == "urn:ogf:network:example.net:2025:src?vlan=100"
        assert r.dest_stp == "urn:ogf:network:example.net:2025:dst?vlan=200"
        assert r.connection_states.reservation_state == "ReserveStart"
        assert r.connection_states.provision_state == "Provisioned"
        assert r.connection_states.lifecycle_state == "Created"
        assert r.connection_states.data_plane_active is True

    def test_reservation_without_criteria(self) -> None:
        xml = _build_xml(_RESERVATION_WITHOUT_CRITERIA)
        result = parse_query_summary_sync(xml)
        assert len(result) == 1
        r = result[0]
        assert r.connection_id == "conn-002"
        assert r.criteria_version is None
        assert r.capacity is None
        assert r.source_stp is None
        assert r.dest_stp is None
        assert r.connection_states.reservation_state == "ReserveTimeout"
        assert r.connection_states.data_plane_active is False

    def test_multiple_reservations(self) -> None:
        xml = _build_xml(_RESERVATION_WITH_CRITERIA, _RESERVATION_WITHOUT_CRITERIA)
        result = parse_query_summary_sync(xml)
        assert len(result) == 2
        assert result[0].connection_id == "conn-001"
        assert result[1].connection_id == "conn-002"

    def test_real_world_xml(self) -> None:
        """Parse the example output from the actual aggregator."""
        with open("query.summary.sync.example.output.xml", "rb") as f:
            xml = f.read()
        result = parse_query_summary_sync(xml)
        assert len(result) > 0
        ids = {r.connection_id for r in result}
        assert "250660f8-266a-465f-8eeb-0317c2bea95b" in ids

    def test_wrong_operation_raises(self) -> None:
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
            ' xmlns:nsi_ctypes="http://schemas.ogf.org/nsi/2013/12/connection/types">'
            "<soapenv:Body>"
            "<nsi_ctypes:reserveResponse><connectionId>x</connectionId></nsi_ctypes:reserveResponse>"
            "</soapenv:Body>"
            "</soapenv:Envelope>"
        ).encode()
        with pytest.raises(ValueError, match="Expected querySummarySyncConfirmed"):
            parse_query_summary_sync(xml)
