"""Incoming NSI CS v2 SOAP callback endpoint.

The aggregator POSTs async messages here (reserveConfirmed,
reserveCommitConfirmed, dataPlaneStateChange, etc.).  Each message is parsed,
the correlationId is extracted, and the matching pending Future in the
ReservationStore is resolved so the waiting background task can proceed.
"""

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from aggregator_proxy.dependencies import get_reservation_store
from aggregator_proxy.nsi_soap import DataPlaneStateChange, parse, parse_correlation_id
from aggregator_proxy.reservation_store import ReservationStore

logger = structlog.get_logger(__name__)

NSI_CALLBACK_PATH = "/nsi/v2/callback"

router = APIRouter(tags=["nsi-callback"])


@router.post(NSI_CALLBACK_PATH, status_code=200, include_in_schema=False)
async def nsi_callback(
    request: Request,
    store: ReservationStore = Depends(get_reservation_store),
) -> Response:
    """Receive an async NSI callback from the aggregator and dispatch it."""
    xml_bytes = await request.body()
    logger.debug("Inbound NSI callback XML", xml=xml_bytes.decode(errors="replace"))

    try:
        correlation_id = parse_correlation_id(xml_bytes)
        message = parse(xml_bytes)
    except ValueError:
        logger.exception("Failed to parse incoming NSI callback")
        return Response(status_code=400)

    resolved = store.resolve_pending(correlation_id, message)

    if isinstance(message, DataPlaneStateChange):
        resolved = store.resolve_pending_by_connection(message.connection_id, message) or resolved

    if not resolved:
        logger.warning(
            "Received NSI callback for unknown correlationId",
            correlation_id=correlation_id,
            message_type=type(message).__name__,
        )

    return Response(status_code=200)
