"""Reservation API endpoints."""

import httpx
import structlog
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from aggregator_proxy.dependencies import get_nsi_client
from aggregator_proxy.models import (
    AcceptedResponse,
    CallbackRequest,
    ReservationDetail,
    ReservationRequest,
    ReservationsListResponse,
)

logger = structlog.get_logger(__name__)

ACCEPTED_TYPE = "https://github.com/workfloworchestrator/nsi-aggregator-proxy#202-accepted"

router = APIRouter(prefix="/reservations", tags=["reservations"])


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedResponse,
    summary="Reserve a connection",
)
async def create_reservation(
    body: ReservationRequest,
    nsi_client: httpx.AsyncClient = Depends(get_nsi_client),
) -> JSONResponse:
    """Reserve a connection using the parameters from the input payload.

    On acceptance the reservation transitions to the ``RESERVING`` state.
    The final result (``RESERVED`` or ``FAILED``) is delivered to
    ``callbackURL``.
    """
    logger.info(
        "Reserve request received",
        description=body.description,
        global_reservation_id=body.globalReservationId,
        callback_url=str(body.callbackURL),
    )

    # TODO: persist reservation, send NSI reserve request to aggregator
    connection_id = "TODO"

    response = AcceptedResponse(
        type=ACCEPTED_TYPE,
        instance=f"/reservations/{connection_id}",
    )
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=response.model_dump())


@router.post(
    "/{connectionId}/provision",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedResponse,
    summary="Provision a reserved connection",
)
async def provision_reservation(
    connectionId: str,
    body: CallbackRequest,
    nsi_client: httpx.AsyncClient = Depends(get_nsi_client),
) -> JSONResponse:
    """Provision the connection identified by ``connectionId``.

    Only allowed when the reservation is in the ``RESERVED`` state.
    On acceptance it transitions to ``ACTIVATING``.  The final result
    (``ACTIVATED`` or ``FAILED``) is delivered to ``callbackURL``.
    """
    logger.info("Provision request received", connection_id=connectionId, callback_url=str(body.callbackURL))

    # TODO: validate state, send NSI provision request to aggregator

    response = AcceptedResponse(
        type=ACCEPTED_TYPE,
        instance=f"/reservations/{connectionId}",
    )
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=response.model_dump())


@router.post(
    "/{connectionId}/release",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedResponse,
    summary="Release an activated connection",
)
async def release_reservation(
    connectionId: str,
    body: CallbackRequest,
    nsi_client: httpx.AsyncClient = Depends(get_nsi_client),
) -> JSONResponse:
    """Release the connection identified by ``connectionId``.

    Only allowed when the reservation is in the ``ACTIVATED`` state.
    On acceptance it transitions to ``DEACTIVATING``.  The final result
    (``RESERVED`` or ``FAILED``) is delivered to ``callbackURL``.
    """
    logger.info("Release request received", connection_id=connectionId, callback_url=str(body.callbackURL))

    # TODO: validate state, send NSI release request to aggregator

    response = AcceptedResponse(
        type=ACCEPTED_TYPE,
        instance=f"/reservations/{connectionId}",
    )
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=response.model_dump())


@router.delete(
    "/{connectionId}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedResponse,
    summary="Terminate a connection",
)
async def terminate_reservation(
    connectionId: str,
    body: CallbackRequest,
    nsi_client: httpx.AsyncClient = Depends(get_nsi_client),
) -> JSONResponse:
    """Terminate the connection identified by ``connectionId``.

    Only allowed when the reservation is in the ``RESERVED`` or ``FAILED``
    state.  On acceptance it transitions to ``TERMINATED``.  The final result
    is delivered to ``callbackURL``.
    """
    logger.info("Terminate request received", connection_id=connectionId, callback_url=str(body.callbackURL))

    # TODO: validate state, send NSI terminate request to aggregator

    response = AcceptedResponse(
        type=ACCEPTED_TYPE,
        instance=f"/reservations/{connectionId}",
    )
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=response.model_dump())


@router.get(
    "/{connectionId}",
    response_model=ReservationDetail,
    summary="Get reservation details",
)
async def get_reservation(
    connectionId: str,
    nsi_client: httpx.AsyncClient = Depends(get_nsi_client),
) -> ReservationDetail:
    """Return the details of the reservation identified by ``connectionId``."""
    logger.info("Get reservation", connection_id=connectionId)

    # TODO: look up reservation from storage
    raise NotImplementedError


@router.get(
    "",
    response_model=ReservationsListResponse,
    summary="List all reservations",
)
async def list_reservations(
    nsi_client: httpx.AsyncClient = Depends(get_nsi_client),
) -> ReservationsListResponse:
    """Return a list of all reservations and their details."""
    logger.info("List all reservations")

    # TODO: retrieve all reservations from storage
    return ReservationsListResponse(reservations=[])
