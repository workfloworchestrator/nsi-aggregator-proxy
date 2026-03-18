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


"""Tests for build_query_notification_sync."""

from lxml import etree

from aggregator_proxy.nsi_soap.builder import NsiHeader, build_query_notification_sync
from aggregator_proxy.nsi_soap.namespaces import NSMAP

_C = NSMAP["nsi_ctypes"]
_S = NSMAP["soapenv"]


def test_build_query_notification_sync_structure() -> None:
    header = NsiHeader(
        requester_nsa="urn:ogf:network:example.net:2025:nsa:requester",
        provider_nsa="urn:ogf:network:example.net:2025:nsa:provider",
        reply_to="http://proxy.test/nsi/v2/callback",
        correlation_id="urn:uuid:test-corr-id",
    )
    xml_bytes = build_query_notification_sync(header, "conn-001")

    root = etree.fromstring(xml_bytes)
    body = root.find(f"{{{_S}}}Body")
    assert body is not None

    qns = body.find(f"{{{_C}}}queryNotificationSync")
    assert qns is not None

    assert qns.findtext("connectionId") == "conn-001"
    assert qns.findtext("startNotificationId") == "1"
    assert qns.findtext("endNotificationId") == "2147483647"
