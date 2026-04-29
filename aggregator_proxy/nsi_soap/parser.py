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


"""Parser for inbound NSI CS v2 SOAP response and callback messages.

Message classification
----------------------
Synchronous (returned in the HTTP response body):
  - ReserveResponse   — immediate reply to reserve, carries the connectionId
  - Acknowledgment    — immediate reply to provision, release and terminate

Asynchronous (POSTed to the replyTo URL):
  - ReserveConfirmed        — reserve succeeded, ready for commit
  - ReserveFailed           — reserve failed; state → FAILED
  - ReserveTimeout          — reserve timed out before commit; state → FAILED
  - ReserveCommitConfirmed  — commit succeeded, state is RESERVED
  - ReserveCommitFailed     — commit failed; state → FAILED
  - ProvisionConfirmed      — provision request accepted by downstream
  - DataPlaneStateChange    — data plane came up (active=True) or went down
  - ReleaseConfirmed        — release succeeded, state returns to RESERVED
  - TerminateConfirmed      — connection terminated
"""

from __future__ import annotations

from dataclasses import dataclass

from lxml import etree

from aggregator_proxy.nsi_soap.namespaces import NSMAP

XmlInput = bytes | etree._Element

_C = NSMAP["nsi_ctypes"]
_P = NSMAP["nsi_p2p"]


# ---------------------------------------------------------------------------
# Synchronous responses
# ---------------------------------------------------------------------------


@dataclass
class ReserveResponse:
    """Synchronous response to reserve — carries the aggregator-assigned connectionId."""

    connection_id: str


@dataclass
class Acknowledgment:
    """Synchronous acknowledgement returned for provision, release and terminate."""


# ---------------------------------------------------------------------------
# Asynchronous callbacks
# ---------------------------------------------------------------------------


@dataclass
class ReserveConfirmed:
    """Reservation held by the aggregator; proxy must send reserveCommit."""

    connection_id: str
    criteria_version: int
    service_type: str
    capacity: int
    source_stp: str
    dest_stp: str


@dataclass
class Variable:
    """A typed variable from a serviceException's variables list."""

    type: str
    value: str


@dataclass
class ServiceException:
    """Error detail carried inside a reserveFailed message (GFD.235).

    The top-level exception from an aggregator may omit ``connection_id``
    and carry the real error in one or more ``child_exceptions``.
    """

    nsa_id: str
    connection_id: str | None
    error_id: str
    text: str
    variables: list[Variable] | None = None
    child_exceptions: list["ServiceException"] | None = None


@dataclass
class ReserveFailed:
    """Reservation failed; state → FAILED."""

    connection_id: str
    service_exception: ServiceException


@dataclass
class ReserveTimeout:
    """Reservation timed out before commit was sent; state → FAILED."""

    connection_id: str
    notification_id: int
    timestamp: str
    timeout_value: int
    originating_connection_id: str
    originating_nsa: str


@dataclass
class ReserveCommitFailed:
    """Commit failed; state → FAILED."""

    connection_id: str
    service_exception: ServiceException


@dataclass
class ReserveCommitConfirmed:
    """Reservation committed; state is now RESERVED."""

    connection_id: str


@dataclass
class ProvisionConfirmed:
    """Provision request accepted by the aggregator; awaiting dataPlaneStateChange."""

    connection_id: str


@dataclass
class DataPlaneStateChange:
    """Data plane came up (active=True) or went down (active=False)."""

    connection_id: str
    notification_id: int
    timestamp: str
    active: bool
    version: int
    version_consistent: bool


@dataclass
class ReleaseConfirmed:
    """Data plane released; state returns to RESERVED."""

    connection_id: str


@dataclass
class TerminateConfirmed:
    """Connection terminated."""

    connection_id: str


@dataclass
class QueryRecursiveResult:
    """Wrapper for queryRecursiveConfirmed callback carrying parsed reservations."""

    reservations: list[QueryReservation]


NsiMessage = (
    ReserveResponse
    | Acknowledgment
    | ReserveConfirmed
    | ReserveFailed
    | ReserveTimeout
    | ReserveCommitFailed
    | ReserveCommitConfirmed
    | ProvisionConfirmed
    | DataPlaneStateChange
    | ReleaseConfirmed
    | TerminateConfirmed
    | QueryRecursiveResult
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require(element: etree._Element, tag: str) -> str:
    """Return the text of a direct child element, raising ValueError if absent."""
    result = element.findtext(tag)
    if result is None:
        raise ValueError(f"Required element <{tag}> not found inside <{etree.QName(element.tag).localname}>")
    return result


def _parse_service_exception(exc_el: etree._Element) -> ServiceException:
    """Parse a serviceException (or childException) element recursively."""
    children = [_parse_service_exception(child_el) for child_el in exc_el.findall("childException")]

    variables_el = exc_el.find("variables")
    variables = [
        Variable(type=var_el.get("type", ""), value=var_el.findtext("value") or "")
        for var_el in (variables_el.findall("variable") if variables_el is not None else [])
        if var_el.get("type", "") or var_el.findtext("value") or ""
    ]

    return ServiceException(
        nsa_id=_require(exc_el, "nsaId"),
        connection_id=exc_el.findtext("connectionId"),
        error_id=_require(exc_el, "errorId"),
        text=_require(exc_el, "text"),
        variables=variables or None,
        child_exceptions=children or None,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse(xml: XmlInput) -> NsiMessage:
    """Parse any NSI SOAP response or callback and return a typed dataclass."""
    root = etree.fromstring(xml) if isinstance(xml, bytes) else xml
    body = root.find(f"{{{NSMAP['soapenv']}}}Body")
    if body is None or not len(body):
        raise ValueError("No SOAP Body found or Body is empty")

    operation = body[0]
    local = etree.QName(operation.tag).localname

    match local:
        case "reserveResponse":
            return ReserveResponse(
                connection_id=_require(operation, "connectionId"),
            )

        case "reserveConfirmed":
            criteria = operation.find("criteria")
            if criteria is None:
                raise ValueError("<criteria> not found in reserveConfirmed")
            p2ps = criteria.find(f"{{{_P}}}p2ps")
            if p2ps is None:
                raise ValueError("<p2ps> not found in criteria")
            return ReserveConfirmed(
                connection_id=_require(operation, "connectionId"),
                criteria_version=int(criteria.get("version", "1")),
                service_type=_require(criteria, "serviceType"),
                capacity=int(_require(p2ps, "capacity")),
                source_stp=_require(p2ps, "sourceSTP"),
                dest_stp=_require(p2ps, "destSTP"),
            )

        case "reserveFailed":
            exc = operation.find("serviceException")
            if exc is None:
                raise ValueError("<serviceException> not found in reserveFailed")
            return ReserveFailed(
                connection_id=_require(operation, "connectionId"),
                service_exception=_parse_service_exception(exc),
            )

        case "reserveTimeout":
            return ReserveTimeout(
                connection_id=_require(operation, "connectionId"),
                notification_id=int(_require(operation, "notificationId")),
                timestamp=_require(operation, "timeStamp"),
                timeout_value=int(_require(operation, "timeoutValue")),
                originating_connection_id=_require(operation, "originatingConnectionId"),
                originating_nsa=_require(operation, "originatingNSA"),
            )

        case "reserveCommitFailed":
            exc = operation.find("serviceException")
            if exc is None:
                raise ValueError("<serviceException> not found in reserveCommitFailed")
            return ReserveCommitFailed(
                connection_id=_require(operation, "connectionId"),
                service_exception=_parse_service_exception(exc),
            )

        case "reserveCommitConfirmed":
            return ReserveCommitConfirmed(
                connection_id=_require(operation, "connectionId"),
            )

        case "provisionConfirmed":
            return ProvisionConfirmed(
                connection_id=_require(operation, "connectionId"),
            )

        case "dataPlaneStateChange":
            dps = operation.find("dataPlaneStatus")
            if dps is None:
                raise ValueError("<dataPlaneStatus> not found in dataPlaneStateChange")
            return DataPlaneStateChange(
                connection_id=_require(operation, "connectionId"),
                notification_id=int(_require(operation, "notificationId")),
                timestamp=_require(operation, "timeStamp"),
                active=_require(dps, "active") == "true",
                version=int(_require(dps, "version")),
                version_consistent=_require(dps, "versionConsistent") == "true",
            )

        case "releaseConfirmed":
            return ReleaseConfirmed(
                connection_id=_require(operation, "connectionId"),
            )

        case "terminateConfirmed":
            return TerminateConfirmed(
                connection_id=_require(operation, "connectionId"),
            )

        case "acknowledgment":
            return Acknowledgment()

        case "queryRecursiveConfirmed":
            return QueryRecursiveResult(reservations=_parse_reservations(operation))

        case _:
            raise ValueError(f"Unknown NSI operation: {local!r}")


# ---------------------------------------------------------------------------
# Query summary sync
# ---------------------------------------------------------------------------


@dataclass
class ConnectionStates:
    """NSI connection sub-state machines."""

    reservation_state: str
    provision_state: str
    lifecycle_state: str
    data_plane_active: bool


@dataclass
class ChildSegment:
    """A child path segment from a querySummarySync or queryRecursiveConfirmed response."""

    order: int
    connection_id: str
    provider_nsa: str
    service_type: str | None = None
    capacity: int | None = None
    source_stp: str | None = None
    dest_stp: str | None = None
    connection_states: ConnectionStates | None = None


@dataclass
class QueryReservation:
    """A single reservation as returned in querySummarySyncConfirmed or queryRecursiveConfirmed."""

    connection_id: str
    global_reservation_id: str | None
    description: str
    requester_nsa: str
    connection_states: ConnectionStates
    criteria_version: int | None = None
    service_type: str | None = None
    capacity: int | None = None
    source_stp: str | None = None
    dest_stp: str | None = None
    children: list[ChildSegment] | None = None


def _parse_connection_states(parent: etree._Element, context: str) -> ConnectionStates:
    """Parse a <connectionStates> element from a reservation or child element."""
    states_el = parent.find("connectionStates")
    if states_el is None:
        raise ValueError(f"<connectionStates> not found for {context}")
    dps_el = states_el.find("dataPlaneStatus")
    if dps_el is None:
        raise ValueError(f"<dataPlaneStatus> not found for {context}")
    return ConnectionStates(
        reservation_state=_require(states_el, "reservationState"),
        provision_state=_require(states_el, "provisionState"),
        lifecycle_state=_require(states_el, "lifecycleState"),
        data_plane_active=_require(dps_el, "active") == "true",
    )


def _parse_child_element(child_el: etree._Element) -> ChildSegment:
    """Parse a <child> element from either summary or recursive results."""
    connection_states: ConnectionStates | None = None
    if child_el.find("connectionStates") is not None:
        connection_states = _parse_connection_states(child_el, f"child {child_el.get('order', '?')}")

    p2ps_el = child_el.find(f"{{{_P}}}p2ps")
    if p2ps_el is None:
        criteria_el = child_el.find("criteria")
        if criteria_el is not None:
            p2ps_el = criteria_el.find(f"{{{_P}}}p2ps")

    cap_text = p2ps_el.findtext("capacity") if p2ps_el is not None else None

    return ChildSegment(
        order=int(child_el.get("order", "0")),
        connection_id=_require(child_el, "connectionId"),
        provider_nsa=_require(child_el, "providerNSA"),
        service_type=child_el.findtext("serviceType"),
        capacity=int(cap_text) if cap_text is not None else None,
        source_stp=p2ps_el.findtext("sourceSTP") if p2ps_el is not None else None,
        dest_stp=p2ps_el.findtext("destSTP") if p2ps_el is not None else None,
        connection_states=connection_states,
    )


def _parse_reservations(confirmed: etree._Element) -> list[QueryReservation]:
    """Parse <reservation> elements from a querySummarySyncConfirmed or queryRecursiveConfirmed."""
    return [_parse_reservation_element(reservation_el) for reservation_el in confirmed.findall("reservation")]


def _parse_reservation_element(reservation_el: etree._Element) -> QueryReservation:
    """Parse a single <reservation> element into a QueryReservation."""
    connection_id = _require(reservation_el, "connectionId")
    connection_states = _parse_connection_states(reservation_el, f"reservation {connection_id}")

    criteria_version: int | None = None
    service_type: str | None = None
    capacity: int | None = None
    source_stp: str | None = None
    dest_stp: str | None = None
    children: list[ChildSegment] | None = None

    criteria_el = reservation_el.find("criteria")
    if criteria_el is not None:
        criteria_version = int(criteria_el.get("version", "1"))
        service_type = criteria_el.findtext("serviceType")
        p2ps_el = criteria_el.find(f"{{{_P}}}p2ps")
        if p2ps_el is not None:
            cap_text = p2ps_el.findtext("capacity")
            if cap_text is not None:
                capacity = int(cap_text)
            source_stp = p2ps_el.findtext("sourceSTP")
            dest_stp = p2ps_el.findtext("destSTP")

        children_el = criteria_el.find("children")
        if children_el is not None:
            children = [_parse_child_element(child_el) for child_el in children_el.findall("child")]
            children = children or None

    return QueryReservation(
        connection_id=connection_id,
        global_reservation_id=reservation_el.findtext("globalReservationId"),
        description=reservation_el.findtext("description") or "",
        requester_nsa=reservation_el.findtext("requesterNSA") or "",
        connection_states=connection_states,
        criteria_version=criteria_version,
        service_type=service_type,
        capacity=capacity,
        source_stp=source_stp,
        dest_stp=dest_stp,
        children=children,
    )


def parse_query_summary_sync(xml_bytes: bytes) -> list[QueryReservation]:
    """Parse a querySummarySyncConfirmed SOAP envelope into a list of reservations."""
    root = etree.fromstring(xml_bytes)
    body = root.find(f"{{{NSMAP['soapenv']}}}Body")
    if body is None or not len(body):
        raise ValueError("No SOAP Body found or Body is empty")

    confirmed = body[0]
    local = etree.QName(confirmed.tag).localname
    if local != "querySummarySyncConfirmed":
        raise ValueError(f"Expected querySummarySyncConfirmed, got {local!r}")

    return _parse_reservations(confirmed)


# ---------------------------------------------------------------------------
# Query notification sync
# ---------------------------------------------------------------------------


@dataclass
class ErrorEvent:
    """An errorEvent notification from queryNotificationSync."""

    connection_id: str
    notification_id: int
    timestamp: str
    event: str  # activateFailed | deactivateFailed | dataplaneError | forcedEnd
    originating_connection_id: str
    originating_nsa: str
    service_exception: ServiceException | None


def parse_query_notification_sync(xml_bytes: bytes) -> list[ErrorEvent]:
    """Parse a queryNotificationSyncConfirmed SOAP envelope into a list of error events."""
    root = etree.fromstring(xml_bytes)
    body = root.find(f"{{{NSMAP['soapenv']}}}Body")
    if body is None or not len(body):
        raise ValueError("No SOAP Body found or Body is empty")

    confirmed = body[0]
    local = etree.QName(confirmed.tag).localname
    if local != "queryNotificationSyncConfirmed":
        raise ValueError(f"Expected queryNotificationSyncConfirmed, got {local!r}")

    results: list[ErrorEvent] = []
    for error_el in confirmed.findall(f"{{{_C}}}errorEvent"):
        connection_id = _require(error_el, "connectionId")
        notification_id = int(_require(error_el, "notificationId"))
        timestamp = _require(error_el, "timeStamp")
        event = _require(error_el, "event")
        originating_connection_id = _require(error_el, "originatingConnectionId")
        originating_nsa = _require(error_el, "originatingNSA")

        exc_el = error_el.find("serviceException")
        service_exception = _parse_service_exception(exc_el) if exc_el is not None else None

        results.append(
            ErrorEvent(
                connection_id=connection_id,
                notification_id=notification_id,
                timestamp=timestamp,
                event=event,
                originating_connection_id=originating_connection_id,
                originating_nsa=originating_nsa,
                service_exception=service_exception,
            )
        )

    return results


def parse_correlation_id(xml: XmlInput) -> str:
    """Extract the correlationId from the SOAP nsiHeader.

    Uses local-name() XPath to be robust against namespace prefix variations
    across different NSI aggregator implementations.
    """
    root = etree.fromstring(xml) if isinstance(xml, bytes) else xml
    results: list[str] = root.xpath(  # type: ignore[assignment]
        "//*[local-name()='nsiHeader']/*[local-name()='correlationId']/text()"
    )
    if not results:
        raise ValueError("correlationId not found in SOAP nsiHeader")
    return str(results[0])
