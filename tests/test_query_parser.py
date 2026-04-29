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


"""Tests for parse_query_summary_sync."""

import pytest

from aggregator_proxy.nsi_soap.parser import ChildSegment, parse_query_summary_sync

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


_CHILD_A = (
    '<child order="0">'
    "<connectionId>child-a</connectionId>"
    "<providerNSA>urn:ogf:network:west.example.net:2025:nsa:supa</providerNSA>"
    "<serviceType>http://services.ogf.org/nsi/2013/12/descriptions/EVTS.A-GOLE</serviceType>"
    '<nsi_p2p:p2ps xmlns:nsi_p2p="http://schemas.ogf.org/nsi/2013/12/services/point2point">'
    "<capacity>1000</capacity>"
    "<sourceSTP>urn:ogf:network:west.example.net:2025:port-a?vlan=100</sourceSTP>"
    "<destSTP>urn:ogf:network:west.example.net:2025:port-b?vlan=200</destSTP>"
    "</nsi_p2p:p2ps>"
    "</child>"
)

_CHILD_B = (
    '<child order="1">'
    "<connectionId>child-b</connectionId>"
    "<providerNSA>urn:ogf:network:east.example.net:2025:nsa:supa</providerNSA>"
    "<serviceType>http://services.ogf.org/nsi/2013/12/descriptions/EVTS.A-GOLE</serviceType>"
    '<nsi_p2p:p2ps xmlns:nsi_p2p="http://schemas.ogf.org/nsi/2013/12/services/point2point">'
    "<capacity>1000</capacity>"
    "<sourceSTP>urn:ogf:network:east.example.net:2025:port-c?vlan=200</sourceSTP>"
    "<destSTP>urn:ogf:network:east.example.net:2025:port-d?vlan=300</destSTP>"
    "</nsi_p2p:p2ps>"
    "</child>"
)

_CHILD_NO_P2PS = (
    '<child order="2">'
    "<connectionId>child-nop2ps</connectionId>"
    "<providerNSA>urn:ogf:network:mid.example.net:2025:nsa:supa</providerNSA>"
    "</child>"
)

_RESERVATION_WITH_CHILDREN = (
    "<reservation>"
    "<connectionId>conn-children</connectionId>"
    "<description>reservation with children</description>"
    '<criteria version="1">'
    "<serviceType>http://services.ogf.org/nsi/2013/12/descriptions/EVTS.A-GOLE</serviceType>"
    f"<children>{_CHILD_A}{_CHILD_B}</children>"
    '<nsi_p2p:p2ps xmlns:nsi_p2p="http://schemas.ogf.org/nsi/2013/12/services/point2point">'
    "<capacity>1000</capacity>"
    "<sourceSTP>urn:ogf:network:example.net:2025:src?vlan=100</sourceSTP>"
    "<destSTP>urn:ogf:network:example.net:2025:dst?vlan=300</destSTP>"
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

_RESERVATION_WITH_EMPTY_CHILDREN = (
    "<reservation>"
    "<connectionId>conn-empty-children</connectionId>"
    "<description>empty children</description>"
    '<criteria version="1">'
    "<children/>"
    '<nsi_p2p:p2ps xmlns:nsi_p2p="http://schemas.ogf.org/nsi/2013/12/services/point2point">'
    "<capacity>100</capacity>"
    "<sourceSTP>urn:ogf:network:example.net:2025:src?vlan=100</sourceSTP>"
    "<destSTP>urn:ogf:network:example.net:2025:dst?vlan=200</destSTP>"
    "</nsi_p2p:p2ps>"
    "</criteria>"
    "<requesterNSA>urn:ogf:network:example.net:2025:nsa:requester</requesterNSA>"
    "<connectionStates>"
    "<reservationState>ReserveStart</reservationState>"
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


class TestParseQuerySummarySyncChildren:
    """Tests for children parsing from querySummarySyncConfirmed."""

    def test_reservation_with_two_children(self) -> None:
        xml = _build_xml(_RESERVATION_WITH_CHILDREN)
        result = parse_query_summary_sync(xml)
        assert len(result) == 1
        r = result[0]
        assert r.children is not None
        assert len(r.children) == 2

        child_a = r.children[0]
        assert child_a == ChildSegment(
            order=0,
            connection_id="child-a",
            provider_nsa="urn:ogf:network:west.example.net:2025:nsa:supa",
            service_type="http://services.ogf.org/nsi/2013/12/descriptions/EVTS.A-GOLE",
            capacity=1000,
            source_stp="urn:ogf:network:west.example.net:2025:port-a?vlan=100",
            dest_stp="urn:ogf:network:west.example.net:2025:port-b?vlan=200",
            connection_states=None,
        )

        child_b = r.children[1]
        assert child_b.order == 1
        assert child_b.connection_id == "child-b"
        assert child_b.provider_nsa == "urn:ogf:network:east.example.net:2025:nsa:supa"
        assert child_b.connection_states is None

    def test_reservation_without_criteria_has_no_children(self) -> None:
        xml = _build_xml(_RESERVATION_WITHOUT_CRITERIA)
        result = parse_query_summary_sync(xml)
        assert result[0].children is None

    def test_reservation_without_children_element(self) -> None:
        xml = _build_xml(_RESERVATION_WITH_CRITERIA)
        result = parse_query_summary_sync(xml)
        assert result[0].children is None

    def test_empty_children_element(self) -> None:
        xml = _build_xml(_RESERVATION_WITH_EMPTY_CHILDREN)
        result = parse_query_summary_sync(xml)
        assert result[0].children is None

    def test_child_without_p2ps(self) -> None:
        reservation_xml = (
            "<reservation>"
            "<connectionId>conn-nop2ps</connectionId>"
            "<description>child without p2ps</description>"
            '<criteria version="1">'
            f"<children>{_CHILD_NO_P2PS}</children>"
            '<nsi_p2p:p2ps xmlns:nsi_p2p="http://schemas.ogf.org/nsi/2013/12/services/point2point">'
            "<capacity>100</capacity>"
            "<sourceSTP>urn:ogf:network:example.net:2025:src?vlan=100</sourceSTP>"
            "<destSTP>urn:ogf:network:example.net:2025:dst?vlan=200</destSTP>"
            "</nsi_p2p:p2ps>"
            "</criteria>"
            "<requesterNSA>urn:ogf:network:example.net:2025:nsa:requester</requesterNSA>"
            "<connectionStates>"
            "<reservationState>ReserveStart</reservationState>"
            "<provisionState>Released</provisionState>"
            "<lifecycleState>Created</lifecycleState>"
            "<dataPlaneStatus><active>false</active><version>0</version>"
            "<versionConsistent>false</versionConsistent></dataPlaneStatus>"
            "</connectionStates>"
            "</reservation>"
        )
        xml = _build_xml(reservation_xml)
        result = parse_query_summary_sync(xml)
        assert result[0].children is not None
        child = result[0].children[0]
        assert child.connection_id == "child-nop2ps"
        assert child.capacity is None
        assert child.source_stp is None
        assert child.dest_stp is None
        assert child.service_type is None
