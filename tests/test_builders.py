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

import pytest
from lxml import etree

from aggregator_proxy.nsi_soap import (
    NsiHeader,
    build_provision,
    build_query_recursive,
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

    def _p2ps_with_ero(self, ero: list[str] | None) -> etree._Element:
        xml = build_reserve(
            header=_header(),
            global_reservation_id=None,
            description="test",
            capacity=100,
            source_stp="urn:ogf:network:example.net:2025:src",
            dest_stp="urn:ogf:network:example.net:2025:dst",
            start_time="2025-06-01T00:00:00.000Z",
            end_time="2045-06-01T00:00:00.000Z",
            ero=ero,
        )
        p2ps = _get_body_operation(_parse_envelope(xml)).find(f"criteria/{{{_P}}}p2ps")
        assert p2ps is not None
        return p2ps

    @pytest.mark.parametrize("ero", [pytest.param(None, id="none"), pytest.param([], id="empty")])
    def test_no_ero_element_when_absent(self, ero: list[str] | None) -> None:
        assert self._p2ps_with_ero(ero).find("ero") is None

    def test_ero_members_are_ordered_and_unqualified(self) -> None:
        """The ero/orderedSTP/stp elements carry no namespace.

        The XSD test cannot catch this: P2PServiceBaseType ends with
        <xsd:any namespace="##other" processContents="lax"/>, so an ero built in the types namespace
        validates clean and is then silently ignored by safnari.
        """
        members = ["urn:ogf:network:a.net:2025:hop-1?vlan=1779", "urn:ogf:network:b.net:2025:hop-2"]
        p2ps = self._p2ps_with_ero(members)

        # find/findall with bare names only match no-namespace elements, so a hit here is the
        # namespace assertion; the XSD test cannot make it (see the docstring).
        ero_element = p2ps.find("ero")
        assert ero_element is not None

        ordered = ero_element.findall("orderedSTP")
        assert [element.get("order") for element in ordered] == ["0", "1"]
        assert [element.findtext("stp") for element in ordered] == members

    def test_ero_follows_dest_stp(self) -> None:
        """The p2p XSD sequence is capacity, directionality, symmetricPath, sourceSTP, destSTP, ero."""
        p2ps = self._p2ps_with_ero(["urn:ogf:network:a.net:2025:hop-1"])
        assert [child.tag for child in p2ps] == [
            "capacity",
            "directionality",
            "symmetricPath",
            "sourceSTP",
            "destSTP",
            "ero",
        ]

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
        assert xml.startswith(b"<?xml version='1.0' encoding='UTF-8'?>")


class TestBuildQueryRecursive:
    def test_with_connection_id(self) -> None:
        xml = build_query_recursive(_header(), connection_id="conn-42")
        root = _parse_envelope(xml)
        op = _get_body_operation(root)
        assert etree.QName(op.tag).localname == "queryRecursive"
        assert op.findtext("connectionId") == "conn-42"

    def test_without_connection_id(self) -> None:
        xml = build_query_recursive(_header())
        root = _parse_envelope(xml)
        op = _get_body_operation(root)
        assert etree.QName(op.tag).localname == "queryRecursive"
        assert op.findtext("connectionId") is None

    def test_correlation_id_roundtrip(self) -> None:
        xml = build_query_recursive(_header("urn:uuid:recursive-corr"), connection_id="conn-42")
        assert parse_correlation_id(xml) == "urn:uuid:recursive-corr"

    def test_header_fields(self) -> None:
        xml = build_query_recursive(_header("urn:uuid:qr-corr"))
        root = _parse_envelope(xml)
        fields = _get_header_fields(root)
        assert fields["correlationId"] == "urn:uuid:qr-corr"
        assert fields["requesterNSA"] == "urn:ogf:network:req:2025:nsa"
        assert fields["providerNSA"] == "urn:ogf:network:prov:2025:nsa"
