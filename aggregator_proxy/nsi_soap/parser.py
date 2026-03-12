"""Parser for inbound NSI CS v2 SOAP response and callback messages.

Message classification
----------------------
Synchronous (returned in the HTTP response body):
  - ReserveResponse   — immediate reply to reserve, carries the connectionId
  - Acknowledgment    — immediate reply to provision, release and terminate

Asynchronous (POSTed to the replyTo URL):
  - ReserveConfirmed        — reserve succeeded, ready for commit
  - ReserveCommitConfirmed  — commit succeeded, state is RESERVED
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
