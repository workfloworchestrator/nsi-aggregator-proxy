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


"""Tests for parse_query_notification_sync."""

import pytest

from aggregator_proxy.nsi_soap.parser import parse_query_notification_sync

_C = "http://schemas.ogf.org/nsi/2013/12/connection/types"

_ENVELOPE_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
    f' xmlns:nsi_ctypes="{_C}">'
    "<soapenv:Body>"
    "<nsi_ctypes:queryNotificationSyncConfirmed>"
    "{content}"
    "</nsi_ctypes:queryNotificationSyncConfirmed>"
    "</soapenv:Body>"
    "</soapenv:Envelope>"
)

_ERROR_EVENT = (
    "<nsi_ctypes:errorEvent>"
    "<connectionId>conn-001</connectionId>"
    "<notificationId>5</notificationId>"
    "<timeStamp>2025-06-01T12:00:00Z</timeStamp>"
    "<event>deactivateFailed</event>"
    "<originatingConnectionId>orig-001</originatingConnectionId>"
    "<originatingNSA>urn:ogf:network:example.net:2025:nsa:child</originatingNSA>"
    "<serviceException>"
    "<nsaId>urn:ogf:network:example.net:2025:nsa:child</nsaId>"
    "<connectionId>orig-001</connectionId>"
    "<errorId>GENERIC_RM_ERROR</errorId>"
    "<text>An internal error</text>"
    "</serviceException>"
    "</nsi_ctypes:errorEvent>"
)

_ERROR_EVENT_NO_EXCEPTION = (
    "<nsi_ctypes:errorEvent>"
    "<connectionId>conn-002</connectionId>"
    "<notificationId>3</notificationId>"
    "<timeStamp>2025-06-01T11:00:00Z</timeStamp>"
    "<event>forcedEnd</event>"
    "<originatingConnectionId>orig-002</originatingConnectionId>"
    "<originatingNSA>urn:ogf:network:example.net:2025:nsa:child</originatingNSA>"
    "</nsi_ctypes:errorEvent>"
)

_DATA_PLANE_STATE_CHANGE = (
    "<nsi_ctypes:dataPlaneStateChange>"
    "<connectionId>conn-001</connectionId>"
    "<notificationId>4</notificationId>"
    "<timeStamp>2025-06-01T11:30:00Z</timeStamp>"
    "<dataPlaneStatus>"
    "<active>true</active>"
    "<version>1</version>"
    "<versionConsistent>true</versionConsistent>"
    "</dataPlaneStatus>"
    "</nsi_ctypes:dataPlaneStateChange>"
)


def _build_xml(content: str) -> bytes:
    return _ENVELOPE_TEMPLATE.format(content=content).encode()


class TestParseQueryNotificationSync:
    """Tests for parse_query_notification_sync."""

    def test_empty_result(self) -> None:
        xml = _build_xml("")
        result = parse_query_notification_sync(xml)
        assert result.error_events == []
        assert result.data_plane_changes == []

    def test_single_error_event(self) -> None:
        xml = _build_xml(_ERROR_EVENT)
        result = parse_query_notification_sync(xml)
        assert len(result.error_events) == 1
        e = result.error_events[0]
        assert e.connection_id == "conn-001"
        assert e.notification_id == 5
        assert e.timestamp == "2025-06-01T12:00:00Z"
        assert e.event == "deactivateFailed"
        assert e.originating_connection_id == "orig-001"
        assert e.originating_nsa == "urn:ogf:network:example.net:2025:nsa:child"
        assert e.service_exception is not None
        assert e.service_exception.error_id == "GENERIC_RM_ERROR"
        assert e.service_exception.text == "An internal error"

    def test_error_event_without_service_exception(self) -> None:
        xml = _build_xml(_ERROR_EVENT_NO_EXCEPTION)
        result = parse_query_notification_sync(xml)
        assert len(result.error_events) == 1
        assert result.error_events[0].event == "forcedEnd"
        assert result.error_events[0].service_exception is None

    def test_multiple_error_events(self) -> None:
        xml = _build_xml(_ERROR_EVENT + _ERROR_EVENT_NO_EXCEPTION)
        result = parse_query_notification_sync(xml)
        assert len(result.error_events) == 2
        assert result.error_events[0].notification_id == 5
        assert result.error_events[1].notification_id == 3

    def test_error_events_and_data_plane_changes_returned(self) -> None:
        """Both errorEvent and dataPlaneStateChange notifications are captured."""
        xml = _build_xml(_ERROR_EVENT + _DATA_PLANE_STATE_CHANGE)
        result = parse_query_notification_sync(xml)
        assert len(result.error_events) == 1
        assert result.error_events[0].event == "deactivateFailed"
        assert len(result.data_plane_changes) == 1
        change = result.data_plane_changes[0]
        assert change.connection_id == "conn-001"
        assert change.notification_id == 4
        assert change.timestamp == "2025-06-01T11:30:00Z"
        assert change.active is True

    def test_wrong_operation_raises(self) -> None:
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
            f' xmlns:nsi_ctypes="{_C}">'
            "<soapenv:Body>"
            "<nsi_ctypes:reserveResponse><connectionId>x</connectionId></nsi_ctypes:reserveResponse>"
            "</soapenv:Body>"
            "</soapenv:Envelope>"
        ).encode()
        with pytest.raises(ValueError, match="Expected queryNotificationSyncConfirmed"):
            parse_query_notification_sync(xml)
