"""Tests for error formatting helpers in reservations router."""

from aggregator_proxy.nsi_soap.parser import ErrorEvent, ServiceException, Variable
from aggregator_proxy.routers.reservations import _format_last_error, _format_service_exception


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


class TestFormatLastError:
    def test_empty_events(self) -> None:
        assert _format_last_error([]) is None

    def test_single_event_with_exception(self) -> None:
        event = ErrorEvent(
            connection_id="conn-1",
            notification_id=1,
            timestamp="2025-06-01T12:00:00Z",
            event="activateFailed",
            originating_connection_id="orig-1",
            originating_nsa="urn:ogf:network:child:2025:nsa",
            service_exception=ServiceException(
                nsa_id="urn:ogf:network:child:2025:nsa",
                connection_id="orig-1",
                error_id="00500",
                text="ACTIVATE_ERROR",
            ),
        )
        result = _format_last_error([event])
        assert result == "activateFailed: 00500: ACTIVATE_ERROR"

    def test_single_event_without_exception(self) -> None:
        event = ErrorEvent(
            connection_id="conn-1",
            notification_id=1,
            timestamp="2025-06-01T12:00:00Z",
            event="forcedEnd",
            originating_connection_id="orig-1",
            originating_nsa="urn:ogf:network:child:2025:nsa",
            service_exception=None,
        )
        result = _format_last_error([event])
        assert result == "forcedEnd"

    def test_multiple_events_returns_latest(self) -> None:
        events = [
            ErrorEvent(
                connection_id="conn-1",
                notification_id=1,
                timestamp="2025-06-01T12:00:00Z",
                event="activateFailed",
                originating_connection_id="orig-1",
                originating_nsa="nsa1",
                service_exception=None,
            ),
            ErrorEvent(
                connection_id="conn-1",
                notification_id=5,
                timestamp="2025-06-01T13:00:00Z",
                event="forcedEnd",
                originating_connection_id="orig-1",
                originating_nsa="nsa1",
                service_exception=None,
            ),
        ]
        result = _format_last_error(events)
        assert result == "forcedEnd"
