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

from dataclasses import dataclass

from lxml import etree

from aggregator_proxy.nsi_soap.namespaces import NSMAP

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
class ServiceException:
    """Error detail carried inside a reserveFailed message (GFD.235)."""

    nsa_id: str
    connection_id: str
    error_id: str
    text: str


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
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require(element: etree._Element, tag: str) -> str:
    """Return the text of a direct child element, raising ValueError if absent."""
    result = element.findtext(tag)
    if result is None:
        raise ValueError(
            f"Required element <{tag}> not found inside "
            f"<{etree.QName(element.tag).localname}>"
        )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse(xml_bytes: bytes) -> NsiMessage:
    """Parse any NSI SOAP response or callback and return a typed dataclass."""
    root = etree.fromstring(xml_bytes)
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
                connection_id   =_require(operation, "connectionId"),
                criteria_version=int(criteria.get("version", "1")),
                service_type    =_require(criteria, "serviceType"),
                capacity        =int(_require(p2ps, "capacity")),
                source_stp      =_require(p2ps, "sourceSTP"),
                dest_stp        =_require(p2ps, "destSTP"),
            )

        case "reserveFailed":
            exc = operation.find("serviceException")
            if exc is None:
                raise ValueError("<serviceException> not found in reserveFailed")
            return ReserveFailed(
                connection_id    =_require(operation, "connectionId"),
                service_exception=ServiceException(
                    nsa_id       =_require(exc, "nsaId"),
                    connection_id=_require(exc, "connectionId"),
                    error_id     =_require(exc, "errorId"),
                    text         =_require(exc, "text"),
                ),
            )

        case "reserveTimeout":
            return ReserveTimeout(
                connection_id           =_require(operation, "connectionId"),
                notification_id         =int(_require(operation, "notificationId")),
                timestamp               =_require(operation, "timeStamp"),
                timeout_value           =int(_require(operation, "timeoutValue")),
                originating_connection_id=_require(operation, "originatingConnectionId"),
                originating_nsa         =_require(operation, "originatingNSA"),
            )

        case "reserveCommitFailed":
            exc = operation.find("serviceException")
            if exc is None:
                raise ValueError("<serviceException> not found in reserveCommitFailed")
            return ReserveCommitFailed(
                connection_id    =_require(operation, "connectionId"),
                service_exception=ServiceException(
                    nsa_id       =_require(exc, "nsaId"),
                    connection_id=_require(exc, "connectionId"),
                    error_id     =_require(exc, "errorId"),
                    text         =_require(exc, "text"),
                ),
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
                connection_id     =_require(operation, "connectionId"),
                notification_id   =int(_require(operation, "notificationId")),
                timestamp         =_require(operation, "timeStamp"),
                active            =_require(dps, "active") == "true",
                version           =int(_require(dps, "version")),
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

        case _:
            raise ValueError(f"Unknown NSI operation: {local!r}")


def parse_correlation_id(xml_bytes: bytes) -> str:
    """Extract the correlationId from the SOAP nsiHeader.

    Uses local-name() XPath to be robust against namespace prefix variations
    across different NSI aggregator implementations.
    """
    root = etree.fromstring(xml_bytes)
    results = root.xpath(
        "//*[local-name()='nsiHeader']/*[local-name()='correlationId']/text()"
    )
    if not results:
        raise ValueError("correlationId not found in SOAP nsiHeader")
    return str(results[0])
