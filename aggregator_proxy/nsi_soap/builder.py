"""Factory functions for outbound NSI CS v2 SOAP request messages."""

from dataclasses import dataclass, field
from uuid import uuid4

from lxml import etree

from aggregator_proxy.nsi_soap.namespaces import NSMAP

_S = "{%s}" % NSMAP["soapenv"]
_H = "{%s}" % NSMAP["nsi_headers"]
_C = "{%s}" % NSMAP["nsi_ctypes"]
_P = "{%s}" % NSMAP["nsi_p2p"]

_PROTOCOL_VERSION = "application/vnd.ogf.nsi.cs.v2.provider+soap"


@dataclass
class NsiHeader:
    """Fields written into the nsiHeader of every outbound NSI SOAP message."""

    requester_nsa: str
    provider_nsa: str
    reply_to: str
    correlation_id: str = field(default_factory=lambda: f"urn:uuid:{uuid4()}")


def _build_envelope(header: NsiHeader) -> tuple[etree._Element, etree._Element]:
    """Return (envelope, body) with the nsiHeader already populated."""
    envelope = etree.Element(f"{_S}Envelope", nsmap=NSMAP)
    soap_header = etree.SubElement(envelope, f"{_S}Header")
    nsi_hdr = etree.SubElement(soap_header, f"{_H}nsiHeader")
    etree.SubElement(nsi_hdr, "protocolVersion").text = _PROTOCOL_VERSION
    etree.SubElement(nsi_hdr, "correlationId").text   = header.correlation_id
    etree.SubElement(nsi_hdr, "requesterNSA").text    = header.requester_nsa
    etree.SubElement(nsi_hdr, "providerNSA").text     = header.provider_nsa
    etree.SubElement(nsi_hdr, "replyTo").text         = header.reply_to
    body = etree.SubElement(envelope, f"{_S}Body")
    return envelope, body


def _serialize(envelope: etree._Element) -> bytes:
    body = etree.tostring(envelope, xml_declaration=False, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>{body}'.encode("UTF-8")


def build_reserve(
    header: NsiHeader,
    global_reservation_id: str | None,
    description: str,
    capacity: int,
    source_stp: str,
    dest_stp: str,
    start_time: str,
    end_time: str,
    service_type: str = "http://services.ogf.org/nsi/2013/12/descriptions/EVTS.A-GOLE",
) -> bytes:
    """Build a NSI reserve request envelope."""
    envelope, body = _build_envelope(header)
    reserve = etree.SubElement(body, f"{_C}reserve")
    if global_reservation_id is not None:
        etree.SubElement(reserve, "globalReservationId").text = global_reservation_id
    etree.SubElement(reserve, "description").text = description
    criteria = etree.SubElement(reserve, "criteria", version="1")
    schedule = etree.SubElement(criteria, "schedule")
    etree.SubElement(schedule, "startTime").text = start_time
    etree.SubElement(schedule, "endTime").text   = end_time
    etree.SubElement(criteria, "serviceType").text = service_type
    p2ps = etree.SubElement(criteria, f"{_P}p2ps")
    etree.SubElement(p2ps, "capacity").text       = str(capacity)
    etree.SubElement(p2ps, "directionality").text = "Bidirectional"
    etree.SubElement(p2ps, "symmetricPath").text  = "true"
    etree.SubElement(p2ps, "sourceSTP").text      = source_stp
    etree.SubElement(p2ps, "destSTP").text        = dest_stp
    return _serialize(envelope)


def _build_connection_id_operation(header: NsiHeader, operation_tag: str, connection_id: str) -> bytes:
    """Build a simple NSI request envelope whose body contains only a connectionId."""
    envelope, body = _build_envelope(header)
    op = etree.SubElement(body, f"{_C}{operation_tag}")
    etree.SubElement(op, "connectionId").text = connection_id
    return _serialize(envelope)


def build_reserve_commit(header: NsiHeader, connection_id: str) -> bytes:
    """Build a NSI reserveCommit request envelope."""
    return _build_connection_id_operation(header, "reserveCommit", connection_id)


def build_provision(header: NsiHeader, connection_id: str) -> bytes:
    """Build a NSI provision request envelope."""
    return _build_connection_id_operation(header, "provision", connection_id)


def build_release(header: NsiHeader, connection_id: str) -> bytes:
    """Build a NSI release request envelope."""
    return _build_connection_id_operation(header, "release", connection_id)


def build_terminate(header: NsiHeader, connection_id: str) -> bytes:
    """Build a NSI terminate request envelope."""
    return _build_connection_id_operation(header, "terminate", connection_id)
