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


"""Tests that validate generated SOAP messages against the official NSI XSD schemas."""

from pathlib import Path

import pytest
from lxml import etree

from aggregator_proxy.nsi_soap.builder import (
    NsiHeader,
    build_provision,
    build_query_notification_sync,
    build_query_recursive,
    build_query_summary_sync,
    build_release,
    build_reserve,
    build_reserve_commit,
    build_terminate,
)

_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "docs" / "schemas"

_HEADER = NsiHeader(
    requester_nsa="urn:ogf:network:example.net:2025:nsa:requester",
    provider_nsa="urn:ogf:network:example.net:2025:nsa:provider",
    reply_to="http://proxy.test/nsi/v2/callback",
    correlation_id="urn:uuid:550e8400-e29b-41d4-a716-446655440000",
)


def _schema_uri(name: str) -> str:
    return (_SCHEMA_DIR / name).as_uri()


@pytest.fixture(scope="module")
def nsi_schema() -> etree.XMLSchema:
    """Build a composite XML Schema covering SOAP envelope + all NSI namespaces."""
    schema_src = (
        '<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema">\n'
        f'  <xsd:import namespace="http://schemas.xmlsoap.org/soap/envelope/"\n'
        f'              schemaLocation="{_schema_uri("envelope.xsd")}"/>\n'
        f'  <xsd:import namespace="http://schemas.ogf.org/nsi/2013/12/framework/headers"\n'
        f'              schemaLocation="{_schema_uri("ogf_nsi_framework_headers_v2_0.xsd")}"/>\n'
        f'  <xsd:import namespace="http://schemas.ogf.org/nsi/2013/12/connection/types"\n'
        f'              schemaLocation="{_schema_uri("ogf_nsi_connection_types_v2_0.xsd")}"/>\n'
        f'  <xsd:import namespace="http://schemas.ogf.org/nsi/2013/12/services/point2point"\n'
        f'              schemaLocation="{_schema_uri("ogf_nsi_services_p2p_v2_0.xsd")}"/>\n'
        "</xsd:schema>\n"
    )
    return etree.XMLSchema(etree.fromstring(schema_src.encode()))


@pytest.mark.parametrize(
    "xml_bytes",
    [
        pytest.param(
            build_reserve(
                header=_HEADER,
                global_reservation_id="urn:uuid:550e8400-e29b-41d4-a716-446655440000",
                description="test circuit",
                capacity=1000,
                source_stp="urn:ogf:network:example.net:2025:src?vlan=100",
                dest_stp="urn:ogf:network:example.net:2025:dst?vlan=200",
                start_time="2025-06-01T00:00:00Z",
                end_time="2045-06-01T00:00:00Z",
            ),
            id="reserve_with_global_id",
        ),
        pytest.param(
            build_reserve(
                header=_HEADER,
                global_reservation_id=None,
                description="test circuit",
                capacity=1000,
                source_stp="urn:ogf:network:example.net:2025:src?vlan=100",
                dest_stp="urn:ogf:network:example.net:2025:dst?vlan=200",
                start_time="2025-06-01T00:00:00Z",
                end_time="2045-06-01T00:00:00Z",
            ),
            id="reserve_without_global_id",
        ),
        pytest.param(build_reserve_commit(_HEADER, "conn-42"), id="reserve_commit"),
        pytest.param(build_provision(_HEADER, "conn-42"), id="provision"),
        pytest.param(build_release(_HEADER, "conn-42"), id="release"),
        pytest.param(build_terminate(_HEADER, "conn-42"), id="terminate"),
        pytest.param(build_query_summary_sync(_HEADER, connection_id="conn-42"), id="query_summary_sync_with_id"),
        pytest.param(build_query_summary_sync(_HEADER), id="query_summary_sync_all"),
        pytest.param(build_query_notification_sync(_HEADER, "conn-42"), id="query_notification_sync"),
        pytest.param(build_query_recursive(_HEADER, connection_id="conn-42"), id="query_recursive_with_id"),
        pytest.param(build_query_recursive(_HEADER), id="query_recursive_all"),
    ],
)
def test_message_validates_against_schema(nsi_schema: etree.XMLSchema, xml_bytes: bytes) -> None:
    """Verify that each generated SOAP message conforms to the NSI XSD schemas."""
    doc = etree.fromstring(xml_bytes)
    nsi_schema.assertValid(doc)


def test_schema_rejects_missing_mandatory_element(nsi_schema: etree.XMLSchema) -> None:
    """Guard against lax pass-through: prove the schema actually catches structural errors."""
    xml_bytes = build_reserve(
        header=_HEADER,
        global_reservation_id=None,
        description="test",
        capacity=100,
        source_stp="urn:ogf:network:example.net:2025:src",
        dest_stp="urn:ogf:network:example.net:2025:dst",
        start_time="2025-06-01T00:00:00Z",
        end_time="2045-06-01T00:00:00Z",
    )
    doc = etree.fromstring(xml_bytes)

    # Remove the mandatory <criteria> element from <reserve>
    ns_c = "http://schemas.ogf.org/nsi/2013/12/connection/types"
    reserve = doc.find(f".//{{{ns_c}}}reserve")
    assert reserve is not None
    criteria = reserve.find("criteria")
    assert criteria is not None
    reserve.remove(criteria)

    assert not nsi_schema.validate(doc)
