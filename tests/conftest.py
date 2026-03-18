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


"""Shared test configuration."""

from __future__ import annotations

import os

# Set required environment variables before any application module is imported.
os.environ.setdefault("AGGREGATOR_PROXY_PROVIDER_URL", "http://aggregator.test/nsi-v2/ConnectionServiceProvider")
os.environ.setdefault("AGGREGATOR_PROXY_BASE_URL", "http://proxy.test")
os.environ.setdefault("AGGREGATOR_PROXY_REQUESTER_NSA", "urn:ogf:network:example.net:2025:nsa:requester")
os.environ.setdefault("AGGREGATOR_PROXY_PROVIDER_NSA", "urn:ogf:network:example.net:2025:nsa:provider")

from aggregator_proxy.nsi_soap.namespaces import NSMAP  # noqa: E402

_C = NSMAP["nsi_ctypes"]
_H = NSMAP["nsi_headers"]
_S = NSMAP["soapenv"]
_P = NSMAP["nsi_p2p"]


def build_query_summary_sync_response(
    connection_id: str,
    correlation_id: str,
    reservation_state: str = "ReserveStart",
    provision_state: str = "Released",
    lifecycle_state: str = "Created",
    data_plane_active: bool = False,
    capacity: int = 1000,
    source_stp: str = "urn:ogf:network:example.net:2025:src?vlan=100",
    dest_stp: str = "urn:ogf:network:example.net:2025:dst?vlan=200",
) -> bytes:
    """Build a querySummarySyncConfirmed SOAP response for a single reservation."""
    active_str = "true" if data_plane_active else "false"
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="{_S}" xmlns:head="{_H}" xmlns:nsi_ctypes="{_C}" xmlns:nsi_p2p="{_P}">
  <soapenv:Header>
    <head:nsiHeader>
      <correlationId>{correlation_id}</correlationId>
    </head:nsiHeader>
  </soapenv:Header>
  <soapenv:Body>
    <nsi_ctypes:querySummarySyncConfirmed>
      <reservation>
        <connectionId>{connection_id}</connectionId>
        <description>test reservation</description>
        <criteria version="1">
          <serviceType>http://services.ogf.org/nsi/2013/12/descriptions/EVTS.A-GOLE</serviceType>
          <nsi_p2p:p2ps>
            <capacity>{capacity}</capacity>
            <sourceSTP>{source_stp}</sourceSTP>
            <destSTP>{dest_stp}</destSTP>
          </nsi_p2p:p2ps>
        </criteria>
        <requesterNSA>urn:ogf:network:example.net:2025:nsa:requester</requesterNSA>
        <connectionStates>
          <reservationState>{reservation_state}</reservationState>
          <provisionState>{provision_state}</provisionState>
          <lifecycleState>{lifecycle_state}</lifecycleState>
          <dataPlaneStatus>
            <active>{active_str}</active>
            <version>1</version>
            <versionConsistent>true</versionConsistent>
          </dataPlaneStatus>
        </connectionStates>
      </reservation>
    </nsi_ctypes:querySummarySyncConfirmed>
  </soapenv:Body>
</soapenv:Envelope>""".encode()


def build_query_notification_sync_response(
    correlation_id: str,
    *error_events: str,
) -> bytes:
    """Build a queryNotificationSyncConfirmed SOAP response with optional errorEvent elements."""
    events_xml = "".join(error_events)
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="{_S}" xmlns:head="{_H}" xmlns:nsi_ctypes="{_C}">
  <soapenv:Header>
    <head:nsiHeader>
      <correlationId>{correlation_id}</correlationId>
    </head:nsiHeader>
  </soapenv:Header>
  <soapenv:Body>
    <nsi_ctypes:queryNotificationSyncConfirmed>{events_xml}</nsi_ctypes:queryNotificationSyncConfirmed>
  </soapenv:Body>
</soapenv:Envelope>""".encode()


def build_error_event_xml(
    connection_id: str = "conn-001",
    notification_id: int = 1,
    timestamp: str = "2025-06-01T12:00:00Z",
    event: str = "deactivateFailed",
    originating_connection_id: str = "orig-conn-001",
    originating_nsa: str = "urn:ogf:network:example.net:2025:nsa:child",
    error_id: str = "GENERIC_RM_ERROR",
    error_text: str = "An internal (N)RM error has caused a failure",
) -> str:
    """Build an errorEvent XML fragment for use in queryNotificationSyncConfirmed."""
    return (
        f'<nsi_ctypes:errorEvent xmlns:nsi_ctypes="{_C}">'
        f"<connectionId>{connection_id}</connectionId>"
        f"<notificationId>{notification_id}</notificationId>"
        f"<timeStamp>{timestamp}</timeStamp>"
        f"<event>{event}</event>"
        f"<originatingConnectionId>{originating_connection_id}</originatingConnectionId>"
        f"<originatingNSA>{originating_nsa}</originatingNSA>"
        f"<serviceException>"
        f"<nsaId>{originating_nsa}</nsaId>"
        f"<connectionId>{originating_connection_id}</connectionId>"
        f"<errorId>{error_id}</errorId>"
        f"<text>{error_text}</text>"
        f"</serviceException>"
        f"</nsi_ctypes:errorEvent>"
    )


def build_empty_query_summary_sync_response(correlation_id: str) -> bytes:
    """Build a querySummarySyncConfirmed SOAP response with no reservations."""
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="{_S}" xmlns:head="{_H}" xmlns:nsi_ctypes="{_C}">
  <soapenv:Header>
    <head:nsiHeader>
      <correlationId>{correlation_id}</correlationId>
    </head:nsiHeader>
  </soapenv:Header>
  <soapenv:Body>
    <nsi_ctypes:querySummarySyncConfirmed/>
  </soapenv:Body>
</soapenv:Envelope>""".encode()
