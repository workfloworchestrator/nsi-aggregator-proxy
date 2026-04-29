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


"""Tests for queryRecursiveConfirmed parsing via the parse() dispatcher."""

import pytest

from aggregator_proxy.nsi_soap.parser import ConnectionStates, QueryRecursiveResult, parse
from tests.conftest import build_child_xml, build_connection_states_xml

_ENVELOPE_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
    ' xmlns:nsi_ctypes="http://schemas.ogf.org/nsi/2013/12/connection/types"'
    ' xmlns:nsi_headers="http://schemas.ogf.org/nsi/2013/12/framework/headers"'
    ' xmlns:nsi_p2p="http://schemas.ogf.org/nsi/2013/12/services/point2point">'
    "<soapenv:Header>"
    "<nsi_headers:nsiHeader>"
    "<correlationId>urn:uuid:test-corr</correlationId>"
    "</nsi_headers:nsiHeader>"
    "</soapenv:Header>"
    "<soapenv:Body>"
    "<nsi_ctypes:queryRecursiveConfirmed>"
    "{reservations}"
    "</nsi_ctypes:queryRecursiveConfirmed>"
    "</soapenv:Body>"
    "</soapenv:Envelope>"
)

_CHILD_WITH_STATES = build_child_xml(
    order=0,
    connection_id="child-recursive-a",
    provider_nsa="urn:ogf:network:west.example.net:2025:nsa:supa",
    source_stp="urn:ogf:network:west.example.net:2025:port-a?vlan=100",
    dest_stp="urn:ogf:network:west.example.net:2025:port-b?vlan=200",
    capacity=1000,
    connection_states=build_connection_states_xml(provision_state="Provisioned", data_plane_active=True),
)

_CHILD_B_WITH_STATES = build_child_xml(
    order=1,
    connection_id="child-recursive-b",
    provider_nsa="urn:ogf:network:east.example.net:2025:nsa:supa",
    source_stp="urn:ogf:network:east.example.net:2025:port-c?vlan=200",
    dest_stp="urn:ogf:network:east.example.net:2025:port-d?vlan=300",
    capacity=1000,
    connection_states=build_connection_states_xml(provision_state="Released", data_plane_active=False),
)

_RESERVATION_WITH_RECURSIVE_CHILDREN = (
    "<reservation>"
    "<connectionId>conn-recursive</connectionId>"
    "<globalReservationId>urn:uuid:550e8400-e29b-41d4-a716-446655440000</globalReservationId>"
    "<description>recursive reservation</description>"
    '<criteria version="1">'
    "<serviceType>http://services.ogf.org/nsi/2013/12/descriptions/EVTS.A-GOLE</serviceType>"
    f"<children>{_CHILD_WITH_STATES}{_CHILD_B_WITH_STATES}</children>"
    "<nsi_p2p:p2ps>"
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


def _build_xml(*reservation_fragments: str) -> bytes:
    return _ENVELOPE_TEMPLATE.format(reservations="".join(reservation_fragments)).encode()


class TestParseQueryRecursiveConfirmed:
    """Tests for queryRecursiveConfirmed dispatched via parse()."""

    def test_dispatches_to_query_recursive_result(self) -> None:
        xml = _build_xml(_RESERVATION_WITH_RECURSIVE_CHILDREN)
        result = parse(xml)
        assert isinstance(result, QueryRecursiveResult)
        assert len(result.reservations) == 1

    def test_reservation_fields(self) -> None:
        xml = _build_xml(_RESERVATION_WITH_RECURSIVE_CHILDREN)
        result = parse(xml)
        assert isinstance(result, QueryRecursiveResult)
        r = result.reservations[0]
        assert r.connection_id == "conn-recursive"
        assert r.global_reservation_id == "urn:uuid:550e8400-e29b-41d4-a716-446655440000"
        assert r.description == "recursive reservation"
        assert r.capacity == 1000
        assert r.connection_states.data_plane_active is True

    def test_children_have_connection_states(self) -> None:
        xml = _build_xml(_RESERVATION_WITH_RECURSIVE_CHILDREN)
        result = parse(xml)
        assert isinstance(result, QueryRecursiveResult)
        children = result.reservations[0].children
        assert children is not None
        assert len(children) == 2

        child_a = children[0]
        assert child_a.order == 0
        assert child_a.connection_id == "child-recursive-a"
        assert child_a.provider_nsa == "urn:ogf:network:west.example.net:2025:nsa:supa"
        assert child_a.capacity == 1000
        assert child_a.source_stp == "urn:ogf:network:west.example.net:2025:port-a?vlan=100"
        assert child_a.connection_states == ConnectionStates(
            reservation_state="ReserveStart",
            provision_state="Provisioned",
            lifecycle_state="Created",
            data_plane_active=True,
        )

        child_b = children[1]
        assert child_b.order == 1
        assert child_b.connection_id == "child-recursive-b"
        assert child_b.connection_states is not None
        assert child_b.connection_states.provision_state == "Released"
        assert child_b.connection_states.data_plane_active is False

    def test_empty_result(self) -> None:
        xml = _build_xml()
        result = parse(xml)
        assert isinstance(result, QueryRecursiveResult)
        assert result.reservations == []

    def test_multiple_reservations(self) -> None:
        second_reservation = (
            "<reservation>"
            "<connectionId>conn-recursive-2</connectionId>"
            "<description>second</description>"
            "<requesterNSA>urn:ogf:network:example.net:2025:nsa:requester</requesterNSA>"
            "<connectionStates>"
            "<reservationState>ReserveTimeout</reservationState>"
            "<provisionState>Released</provisionState>"
            "<lifecycleState>Created</lifecycleState>"
            "<dataPlaneStatus><active>false</active><version>0</version>"
            "<versionConsistent>false</versionConsistent></dataPlaneStatus>"
            "</connectionStates>"
            "</reservation>"
        )
        xml = _build_xml(_RESERVATION_WITH_RECURSIVE_CHILDREN, second_reservation)
        result = parse(xml)
        assert isinstance(result, QueryRecursiveResult)
        assert len(result.reservations) == 2
        assert result.reservations[0].connection_id == "conn-recursive"
        assert result.reservations[1].connection_id == "conn-recursive-2"
        assert result.reservations[1].children is None

    def test_unknown_operation_still_raises(self) -> None:
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
            ' xmlns:nsi_ctypes="http://schemas.ogf.org/nsi/2013/12/connection/types"'
            ' xmlns:nsi_headers="http://schemas.ogf.org/nsi/2013/12/framework/headers">'
            "<soapenv:Header>"
            "<nsi_headers:nsiHeader>"
            "<correlationId>urn:uuid:test</correlationId>"
            "</nsi_headers:nsiHeader>"
            "</soapenv:Header>"
            "<soapenv:Body>"
            "<nsi_ctypes:unknownOperation/>"
            "</soapenv:Body>"
            "</soapenv:Envelope>"
        ).encode()
        with pytest.raises(ValueError, match="Unknown NSI operation"):
            parse(xml)
