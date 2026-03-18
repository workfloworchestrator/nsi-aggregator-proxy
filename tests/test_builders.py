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


"""Tests for nsi_soap builder functions — verify XML structure and roundtrip through parser."""

from lxml import etree

from aggregator_proxy.nsi_soap import (
    NsiHeader,
    build_provision,
    build_query_summary_sync,
    build_release,
    build_reserve,
    build_reserve_commit,
    build_terminate,
    parse_correlation_id,
)
from aggregator_proxy.nsi_soap.namespaces import NSMAP

_S = NSMAP["soapenv"]
_H = NSMAP["nsi_headers"]
_C = NSMAP["nsi_ctypes"]
_P = NSMAP["nsi_p2p"]


def _header(correlation_id: str = "urn:uuid:test-corr") -> NsiHeader:
    return NsiHeader(
        requester_nsa="urn:ogf:network:req:2025:nsa",
        provider_nsa="urn:ogf:network:prov:2025:nsa",
        reply_to="http://proxy.test/nsi/v2/callback",
        correlation_id=correlation_id,
    )


def _parse_envelope(xml_bytes: bytes) -> etree._Element:
    root = etree.fromstring(xml_bytes)
    assert root.tag == f"{{{_S}}}Envelope"
    return root


def _get_body_operation(root: etree._Element) -> etree._Element:
    body = root.find(f"{{{_S}}}Body")
    assert body is not None
    assert len(body) == 1
    return body[0]


def _get_header_fields(root: etree._Element) -> dict[str, str]:
    """Extract nsiHeader fields as a dict."""
    nsi_hdr = root.find(f".//{{{_H}}}nsiHeader")
    assert nsi_hdr is not None
    fields = {}
    for child in nsi_hdr:
        fields[etree.QName(child.tag).localname] = child.text or ""
    return fields


class TestBuildReserve:
    def test_xml_structure(self) -> None:
        xml = build_reserve(
            header=_header(),
            global_reservation_id="urn:uuid:550e8400-e29b-41d4-a716-446655440000",
            description="test circuit",
            capacity=1000,
            source_stp="urn:ogf:network:example.net:2025:src?vlan=100",
            dest_stp="urn:ogf:network:example.net:2025:dst?vlan=200",
            start_time="2025-06-01T00:00:00.000Z",
            end_time="2045-06-01T00:00:00.000Z",
        )
        root = _parse_envelope(xml)
        op = _get_body_operation(root)
        assert etree.QName(op.tag).localname == "reserve"

        assert op.findtext("globalReservationId") == "urn:uuid:550e8400-e29b-41d4-a716-446655440000"
        assert op.findtext("description") == "test circuit"

        criteria = op.find("criteria")
        assert criteria is not None
        assert criteria.get("version") == "1"

        schedule = criteria.find("schedule")
        assert schedule is not None
        assert schedule.findtext("startTime") == "2025-06-01T00:00:00.000Z"
        assert schedule.findtext("endTime") == "2045-06-01T00:00:00.000Z"

        assert criteria.findtext("serviceType") == "http://services.ogf.org/nsi/2013/12/descriptions/EVTS.A-GOLE"

        p2ps = criteria.find(f"{{{_P}}}p2ps")
        assert p2ps is not None
        assert p2ps.findtext("capacity") == "1000"
        assert p2ps.findtext("sourceSTP") == "urn:ogf:network:example.net:2025:src?vlan=100"
        assert p2ps.findtext("destSTP") == "urn:ogf:network:example.net:2025:dst?vlan=200"
        assert p2ps.findtext("directionality") == "Bidirectional"
        assert p2ps.findtext("symmetricPath") == "true"

    def test_without_global_reservation_id(self) -> None:
        xml = build_reserve(
            header=_header(),
            global_reservation_id=None,
            description="test",
            capacity=100,
            source_stp="urn:ogf:network:example.net:2025:src",
            dest_stp="urn:ogf:network:example.net:2025:dst",
            start_time="2025-06-01T00:00:00.000Z",
            end_time="2045-06-01T00:00:00.000Z",
        )
        root = _parse_envelope(xml)
        op = _get_body_operation(root)
        assert op.findtext("globalReservationId") is None

    def test_custom_service_type(self) -> None:
        xml = build_reserve(
            header=_header(),
            global_reservation_id=None,
            description="test",
            capacity=100,
            source_stp="urn:ogf:network:example.net:2025:src",
            dest_stp="urn:ogf:network:example.net:2025:dst",
            start_time="2025-06-01T00:00:00.000Z",
            end_time="2045-06-01T00:00:00.000Z",
            service_type="http://custom/service",
        )
        root = _parse_envelope(xml)
        op = _get_body_operation(root)
        criteria = op.find("criteria")
        assert criteria is not None
        assert criteria.findtext("serviceType") == "http://custom/service"

    def test_header_fields(self) -> None:
        xml = build_reserve(
            header=_header("urn:uuid:my-corr"),
            global_reservation_id=None,
            description="test",
            capacity=100,
            source_stp="urn:ogf:network:example.net:2025:src",
            dest_stp="urn:ogf:network:example.net:2025:dst",
            start_time="2025-06-01T00:00:00.000Z",
            end_time="2045-06-01T00:00:00.000Z",
        )
        root = _parse_envelope(xml)
        fields = _get_header_fields(root)
        assert fields["correlationId"] == "urn:uuid:my-corr"
        assert fields["requesterNSA"] == "urn:ogf:network:req:2025:nsa"
        assert fields["providerNSA"] == "urn:ogf:network:prov:2025:nsa"
        assert fields["replyTo"] == "http://proxy.test/nsi/v2/callback"

    def test_correlation_id_roundtrip(self) -> None:
        xml = build_reserve(
            header=_header("urn:uuid:roundtrip-id"),
            global_reservation_id=None,
            description="test",
            capacity=100,
            source_stp="urn:ogf:network:example.net:2025:src",
            dest_stp="urn:ogf:network:example.net:2025:dst",
            start_time="2025-06-01T00:00:00.000Z",
            end_time="2045-06-01T00:00:00.000Z",
        )
        assert parse_correlation_id(xml) == "urn:uuid:roundtrip-id"


class TestBuildReserveCommit:
    def test_xml_structure(self) -> None:
        xml = build_reserve_commit(_header(), "conn-42")
        root = _parse_envelope(xml)
        op = _get_body_operation(root)
        assert etree.QName(op.tag).localname == "reserveCommit"
        assert op.findtext("connectionId") == "conn-42"

    def test_correlation_id_roundtrip(self) -> None:
        xml = build_reserve_commit(_header("urn:uuid:commit-corr"), "conn-42")
        assert parse_correlation_id(xml) == "urn:uuid:commit-corr"


class TestBuildProvision:
    def test_xml_structure(self) -> None:
        xml = build_provision(_header(), "conn-42")
        root = _parse_envelope(xml)
        op = _get_body_operation(root)
        assert etree.QName(op.tag).localname == "provision"
        assert op.findtext("connectionId") == "conn-42"


class TestBuildRelease:
    def test_xml_structure(self) -> None:
        xml = build_release(_header(), "conn-42")
        root = _parse_envelope(xml)
        op = _get_body_operation(root)
        assert etree.QName(op.tag).localname == "release"
        assert op.findtext("connectionId") == "conn-42"


class TestBuildTerminate:
    def test_xml_structure(self) -> None:
        xml = build_terminate(_header(), "conn-42")
        root = _parse_envelope(xml)
        op = _get_body_operation(root)
        assert etree.QName(op.tag).localname == "terminate"
        assert op.findtext("connectionId") == "conn-42"


class TestBuildQuerySummarySync:
    def test_with_connection_id(self) -> None:
        xml = build_query_summary_sync(_header(), connection_id="conn-42")
        root = _parse_envelope(xml)
        op = _get_body_operation(root)
        assert etree.QName(op.tag).localname == "querySummarySync"
        assert op.findtext("connectionId") == "conn-42"

    def test_without_connection_id(self) -> None:
        xml = build_query_summary_sync(_header())
        root = _parse_envelope(xml)
        op = _get_body_operation(root)
        assert etree.QName(op.tag).localname == "querySummarySync"
        assert op.findtext("connectionId") is None

    def test_xml_declaration_present(self) -> None:
        xml = build_query_summary_sync(_header())
        assert xml.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')
