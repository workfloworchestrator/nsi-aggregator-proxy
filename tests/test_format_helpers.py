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


"""Tests for error formatting helpers in reservations router."""

import pytest

from aggregator_proxy.nsi_soap.parser import Acknowledgment, ErrorEvent, ServiceException, SoapFault, Variable
from aggregator_proxy.routers.reservations import (
    _format_last_error,
    _format_service_exception,
    _sync_failure_detail,
)


class TestFormatServiceException:
    def test_basic_exception(self) -> None:
        exc = ServiceException(
            nsa_id="urn:ogf:network:agg:2025:nsa",
            connection_id="conn-1",
            error_id="00700",
            text="CAPACITY_UNAVAILABLE",
        )
        result = _format_service_exception(exc)
        assert "[00700] CAPACITY_UNAVAILABLE (nsaId=urn:ogf:network:agg:2025:nsa)" == result

    def test_exception_with_variables(self) -> None:
        exc = ServiceException(
            nsa_id="urn:ogf:network:agg:2025:nsa",
            connection_id="conn-1",
            error_id="00700",
            text="CAPACITY_UNAVAILABLE",
            variables=[Variable(type="capacity", value="1000"), Variable(type="available", value="500")],
        )
        result = _format_service_exception(exc)
        assert "capacity=1000" in result
        assert "available=500" in result

    def test_exception_with_child_exceptions(self) -> None:
        exc = ServiceException(
            nsa_id="urn:ogf:network:agg:2025:nsa",
            connection_id=None,
            error_id="00700",
            text="CAPACITY_UNAVAILABLE",
            child_exceptions=[
                ServiceException(
                    nsa_id="urn:ogf:network:child:2025:nsa",
                    connection_id="child-conn-1",
                    error_id="00701",
                    text="No VLAN available",
                ),
            ],
        )
        result = _format_service_exception(exc)
        assert "[00700] CAPACITY_UNAVAILABLE" in result
        assert "child [00701] No VLAN available (nsaId=urn:ogf:network:child:2025:nsa)" in result

    def test_exception_with_child_variables(self) -> None:
        exc = ServiceException(
            nsa_id="urn:ogf:network:agg:2025:nsa",
            connection_id=None,
            error_id="00700",
            text="ERROR",
            child_exceptions=[
                ServiceException(
                    nsa_id="urn:ogf:network:child:2025:nsa",
                    connection_id="child-conn-1",
                    error_id="00701",
                    text="Child error",
                    variables=[Variable(type="port", value="eth0")],
                ),
            ],
        )
        result = _format_service_exception(exc)
        assert "port=eth0" in result

    def test_multiple_children(self) -> None:
        exc = ServiceException(
            nsa_id="urn:ogf:network:agg:2025:nsa",
            connection_id=None,
            error_id="00700",
            text="ERROR",
            child_exceptions=[
                ServiceException(nsa_id="child1", connection_id=None, error_id="001", text="first"),
                ServiceException(nsa_id="child2", connection_id=None, error_id="002", text="second"),
            ],
        )
        result = _format_service_exception(exc)
        assert "child [001] first" in result
        assert "child [002] second" in result


_AGG = "urn:ogf:network:agg:2025:nsa"
_SUPA = "urn:ogf:network:canarie.ca:2025:nsa:supa"


def _event(notification_id: int = 1, event: str = "activateFailed", exc: ServiceException | None = None) -> ErrorEvent:
    return ErrorEvent(
        connection_id="conn-1",
        notification_id=notification_id,
        timestamp="2025-06-01T12:00:00Z",
        event=event,
        originating_connection_id="orig-1",
        originating_nsa=_SUPA,
        service_exception=exc,
    )


def _exc(nsa_id: str, text: str, *children: ServiceException) -> ServiceException:
    return ServiceException(
        nsa_id=nsa_id, connection_id=None, error_id="00800", text=text, child_exceptions=list(children) or None
    )


class TestFormatLastError:
    def test_empty_events(self) -> None:
        assert _format_last_error([]) is None

    @pytest.mark.parametrize(
        ("events", "expected"),
        [
            pytest.param(
                [_event(exc=_exc(_SUPA, "GENERIC_RM_ERROR"))],
                f"activateFailed: 00800: GENERIC_RM_ERROR (nsaId={_SUPA})",
                id="leaf-exception",
            ),
            pytest.param(
                [_event(event="forcedEnd")],
                f"forcedEnd (originatingNSA={_SUPA})",
                id="no-exception",
            ),
            pytest.param(
                [_event(exc=_exc(_AGG, "wrapped copy", _exc(_SUPA, "Could not open socket")))],
                f"activateFailed: 00800: Could not open socket (nsaId={_SUPA})",
                id="aggregator-wraps-child",
            ),
            pytest.param(
                [_event(exc=_exc(_AGG, "wrapped", _exc("urn:a", "first"), _exc("urn:b", "second")))],
                "activateFailed: 00800: first (nsaId=urn:a); 00800: second (nsaId=urn:b)",
                id="several-children",
            ),
            pytest.param(
                [_event(notification_id=1), _event(notification_id=5, event="forcedEnd")],
                f"forcedEnd (originatingNSA={_SUPA})",
                id="latest-event-wins",
            ),
        ],
    )
    def test_format(self, events: list[ErrorEvent], expected: str) -> None:
        assert _format_last_error(events) == expected


class TestSyncFailureDetail:
    """The 502 detail must name the provider's reason, not just the message type."""

    def test_soap_fault_without_detail(self) -> None:
        msg = SoapFault(fault_string="Connection state machine is in invalid state")
        assert _sync_failure_detail(msg) == (
            "Aggregator returned a SOAP Fault: Connection state machine is in invalid state"
        )

    def test_soap_fault_with_service_exception(self) -> None:
        msg = SoapFault(
            fault_string="Error processing request",
            service_exception=ServiceException(
                nsa_id="urn:ogf:network:agg:2025:nsa",
                connection_id=None,
                error_id="00800",
                text="GENERIC_RM_ERROR: teardown failed",
            ),
        )
        assert _sync_failure_detail(msg) == (
            "Aggregator returned a SOAP Fault: Error processing request (00800: GENERIC_RM_ERROR: teardown failed)"
        )

    def test_other_unexpected_message_falls_back_to_the_type_name(self) -> None:
        assert _sync_failure_detail(Acknowledgment()) == "Unexpected sync response from aggregator: Acknowledgment"
