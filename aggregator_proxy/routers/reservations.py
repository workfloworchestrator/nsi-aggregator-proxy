"""Reservation API endpoints."""

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from aggregator_proxy.dependencies import get_callback_client, get_nsi_client, get_reservation_store
from aggregator_proxy.models import (
    AcceptedResponse,
    CallbackRequest,
    CriteriaResponse,
    ReservationDetail,
    ReservationRequest,
    ReservationsListResponse,
    ReservationStatus,
)
from aggregator_proxy.nsi_soap import (
    Acknowledgment,
    DataPlaneStateChange,
    NsiHeader,
    NsiMessage,
    ProvisionConfirmed,
    ReleaseConfirmed,
    ReserveCommitConfirmed,
    ReserveCommitFailed,
    ReserveConfirmed,
    ReserveFailed,
    ReserveResponse,
    ReserveTimeout,
    build_provision,
    build_release,
    build_reserve,
    build_reserve_commit,
    parse,
)
from aggregator_proxy.reservation_store import Reservation, ReservationStore
from aggregator_proxy.routers.nsi_callback import NSI_CALLBACK_PATH
from aggregator_proxy.settings import settings

logger = structlog.get_logger(__name__)

ACCEPTED_TYPE = "https://github.com/workfloworchestrator/nsi-aggregator-proxy#202-accepted"
_SOAP_HEADERS = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'}
_DEFAULT_SERVICE_TYPE = "http://services.ogf.org/nsi/2013/12/descriptions/EVTS.A-GOLE"

router = APIRouter(prefix="/reservations", tags=["reservations"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _send_callback(
    callback_client: httpx.AsyncClient,
    callback_url: str,
    reservation: Reservation,
) -> None:
    """POST reservation detail to the caller's callbackURL."""
    detail = ReservationDetail(
        globalReservationId=reservation.global_reservation_id,
        connectionId=reservation.connection_id,
        description=reservation.description,
        criteria=reservation.criteria,
        status=reservation.status,
    )
    payload = detail.model_dump()
    logger.debug("Outbound JSON callback", callback_url=callback_url, json=payload)
    try:
        await callback_client.post(callback_url, json=payload)
    except Exception as exc:
        logger.error("Failed to deliver callback", callback_url=callback_url, error=str(exc))


async def _complete_reserve(
    connection_id: str,
    reserve_future: asyncio.Future,
    nsi_client: httpx.AsyncClient,
    callback_client: httpx.AsyncClient,
    store: ReservationStore,
) -> None:
    """Background task: drive the reservation to RESERVED or FAILED.

    Phase 1 — wait for reserveConfirmed / reserveFailed / reserveTimeout.
    Phase 2 — send reserveCommit, wait for reserveCommitConfirmed /
               reserveCommitFailed.
    On any failure the state is set to FAILED and the callback is delivered.
    """
    log = logger.bind(connection_id=connection_id)

    async def fail(reason: str) -> None:
        store.update_status(connection_id, ReservationStatus.FAILED)
        reservation = store.get(connection_id)
        if reservation is not None:
            await _send_callback(callback_client, reservation.callback_url, reservation)
        log.info("Reservation failed", reason=reason)

    try:
        # --- Phase 1: wait for reserveConfirmed / failure ---
        try:
            msg = await asyncio.wait_for(reserve_future, timeout=settings.nsi_timeout)
        except asyncio.TimeoutError:
            await fail("no reserveConfirmed received within timeout")
            return

        if isinstance(msg, ReserveFailed):
            await fail(f"reserveFailed [{msg.service_exception.error_id}]: {msg.service_exception.text}")
            return

        if isinstance(msg, ReserveTimeout):
            await fail("reserveTimeout from aggregator")
            return

        assert isinstance(msg, ReserveConfirmed)
        store.update_criteria(connection_id, msg)
        log.info("Reserve confirmed, sending reserveCommit")

        # --- Phase 2: send reserveCommit, wait for commit result ---
        commit_correlation_id = f"urn:uuid:{uuid4()}"
        commit_future = store.register_pending(commit_correlation_id)
        reservation = store.get(connection_id)
        requester_nsa = reservation.requester_nsa if reservation is not None else ""
        provider_nsa = reservation.provider_nsa if reservation is not None else ""
        header = NsiHeader(
            requester_nsa=requester_nsa,
            provider_nsa=provider_nsa,
            reply_to=f"{settings.base_url}{NSI_CALLBACK_PATH}",
            correlation_id=commit_correlation_id,
        )
        soap_bytes = build_reserve_commit(header, connection_id)
        log.debug("Outbound SOAP reserveCommit request", xml=soap_bytes.decode())
        try:
            response = await nsi_client.post(settings.provider_url, content=soap_bytes, headers=_SOAP_HEADERS)
            response.raise_for_status()
        except Exception as exc:
            store.cancel_pending(commit_correlation_id)
            log.error("Failed to send reserveCommit to aggregator", error=str(exc))
            await fail("failed to send reserveCommit to aggregator")
            return

        log.debug("Inbound SOAP reserveCommit response", xml=response.text)

        try:
            commit_msg = await asyncio.wait_for(commit_future, timeout=settings.nsi_timeout)
        except asyncio.TimeoutError:
            await fail("no reserveCommitConfirmed received within timeout")
            return

        if isinstance(commit_msg, ReserveCommitFailed):
            await fail(
                f"reserveCommitFailed [{commit_msg.service_exception.error_id}]: {commit_msg.service_exception.text}"
            )
            return

        assert isinstance(commit_msg, ReserveCommitConfirmed)
        store.update_status(connection_id, ReservationStatus.RESERVED)
        reservation = store.get(connection_id)
        if reservation is not None:
            await _send_callback(callback_client, reservation.callback_url, reservation)
        log.info("Reservation committed, state is RESERVED")

    except Exception:
        log.exception("Unexpected error in _complete_reserve")
        with contextlib.suppress(Exception):
            await fail("internal error")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedResponse,
    summary="Reserve a connection",
)
async def create_reservation(
    body: ReservationRequest,
    nsi_client: httpx.AsyncClient = Depends(get_nsi_client),
    callback_client: httpx.AsyncClient = Depends(get_callback_client),
    store: ReservationStore = Depends(get_reservation_store),
) -> JSONResponse:
    """Reserve a connection using the parameters from the input payload.

    On acceptance the reservation transitions to the ``RESERVING`` state.
    The final result (``RESERVED`` or ``FAILED``) is delivered to
    ``callbackURL``.
    """
    log = logger.bind(
        description=body.description,
        global_reservation_id=body.globalReservationId,
        callback_url=str(body.callbackURL),
    )
    log.info("Reserve request received")
    log.debug("JSON request body", json=body.model_dump(mode="json"))

    correlation_id = f"urn:uuid:{uuid4()}"
    # Register the future BEFORE sending the SOAP request so the callback
    # can never arrive before we are ready to receive it.
    reserve_future = store.register_pending(correlation_id)

    now = datetime.now(timezone.utc)
    header = NsiHeader(
        requester_nsa=body.requesterNSA,
        provider_nsa=body.providerNSA,
        reply_to=f"{settings.base_url}{NSI_CALLBACK_PATH}",
        correlation_id=correlation_id,
    )
    soap_bytes = build_reserve(
        header=header,
        global_reservation_id=body.globalReservationId,
        description=body.description,
        capacity=body.criteria.p2ps.capacity,
        source_stp=body.criteria.p2ps.sourceSTP,
        dest_stp=body.criteria.p2ps.destSTP,
        start_time=now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        end_time=(now + timedelta(days=365 * 20)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        service_type=body.criteria.serviceType or _DEFAULT_SERVICE_TYPE,
    )

    log.debug("Outbound SOAP reserve request", xml=soap_bytes.decode())

    try:
        response = await nsi_client.post(settings.provider_url, content=soap_bytes, headers=_SOAP_HEADERS)
        response.raise_for_status()
    except Exception as exc:
        store.cancel_pending(correlation_id)
        logger.error("Failed to send reserve request to aggregator", error=str(exc))
        raise HTTPException(status_code=502, detail="Failed to reach NSI aggregator")

    log.debug("Inbound SOAP reserve response", xml=response.text)

    sync_msg = parse(response.content)
    if not isinstance(sync_msg, ReserveResponse):
        store.cancel_pending(correlation_id)
        raise HTTPException(
            status_code=502,
            detail=f"Unexpected sync response from aggregator: {type(sync_msg).__name__}",
        )

    connection_id = sync_msg.connection_id
    log = log.bind(connection_id=connection_id)
    log.info("Reserve accepted by aggregator")

    store.create(
        Reservation(
            connection_id=connection_id,
            status=ReservationStatus.RESERVING,
            global_reservation_id=body.globalReservationId,
            description=body.description,
            criteria=CriteriaResponse(
                version=1,
                serviceType=body.criteria.serviceType,
                p2ps=body.criteria.p2ps,
            ),
            requester_nsa=body.requesterNSA,
            provider_nsa=body.providerNSA,
            callback_url=str(body.callbackURL),
        )
    )

    asyncio.create_task(
        _complete_reserve(connection_id, reserve_future, nsi_client, callback_client, store),
        name=f"reserve-{connection_id}",
    )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=AcceptedResponse(
            type=ACCEPTED_TYPE,
            instance=f"/reservations/{connection_id}",
        ).model_dump(),
    )


async def _complete_provision(
    connection_id: str,
    provision_future: asyncio.Future[NsiMessage],
    callback_client: httpx.AsyncClient,
    store: ReservationStore,
) -> None:
    """Background task: drive the reservation from ACTIVATING to ACTIVATED or FAILED.

    Phase 1 — wait for ProvisionConfirmed (timeout: nsi_timeout).
    Phase 2 — loop waiting for DataPlaneStateChange(active=True) (timeout: dataplane_timeout).
    """
    log = logger.bind(connection_id=connection_id)

    async def fail(reason: str) -> None:
        store.update_status(connection_id, ReservationStatus.FAILED)
        reservation = store.get(connection_id)
        if reservation is not None:
            await _send_callback(callback_client, reservation.callback_url, reservation)
        log.info("Provision failed", reason=reason)

    try:
        # --- Phase 1: wait for provisionConfirmed ---
        try:
            msg = await asyncio.wait_for(provision_future, timeout=settings.nsi_timeout)
        except asyncio.TimeoutError:
            await fail("no provisionConfirmed received within timeout")
            return

        if not isinstance(msg, ProvisionConfirmed):
            await fail(f"unexpected message: {type(msg).__name__}")
            return

        log.info("Provision confirmed, waiting for dataPlaneStateChange")

        # --- Phase 2: wait for DataPlaneStateChange(active=True) ---
        remaining = float(settings.dataplane_timeout)
        while remaining > 0:
            dp_future = store.register_pending_by_connection(connection_id)
            start = asyncio.get_event_loop().time()
            try:
                dp_msg = await asyncio.wait_for(dp_future, timeout=remaining)
            except asyncio.TimeoutError:
                store.cancel_pending_by_connection(connection_id)
                await fail("no DataPlaneStateChange(active=True) received within timeout")
                return

            elapsed = asyncio.get_event_loop().time() - start
            remaining -= elapsed

            if not isinstance(dp_msg, DataPlaneStateChange):
                log.warning("Unexpected message while waiting for dataPlaneStateChange", msg_type=type(dp_msg).__name__)
                continue

            if dp_msg.active:
                store.update_status(connection_id, ReservationStatus.ACTIVATED)
                reservation = store.get(connection_id)
                if reservation is not None:
                    await _send_callback(callback_client, reservation.callback_url, reservation)
                log.info("Data plane active, state is ACTIVATED")
                return

            log.info("DataPlaneStateChange active=False, continuing to wait")

        await fail("no DataPlaneStateChange(active=True) received within timeout")

    except Exception:
        log.exception("Unexpected error in _complete_provision")
        with contextlib.suppress(Exception):
            await fail("internal error")


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
    callback_client: httpx.AsyncClient = Depends(get_callback_client),
    store: ReservationStore = Depends(get_reservation_store),
) -> JSONResponse:
    """Provision the connection identified by ``connectionId``.

    Only allowed when the reservation is in the ``RESERVED`` state.
    On acceptance it transitions to ``ACTIVATING``.  The final result
    (``ACTIVATED`` or ``FAILED``) is delivered to ``callbackURL``.
    """
    log = logger.bind(connection_id=connectionId, callback_url=str(body.callbackURL))
    log.info("Provision request received")
    log.debug("JSON request body", json=body.model_dump(mode="json"))

    reservation = store.get(connectionId)
    if reservation is None:
        raise HTTPException(status_code=404, detail=f"Reservation {connectionId!r} not found")
    if reservation.status != ReservationStatus.RESERVED:
        raise HTTPException(
            status_code=409,
            detail=f"Reservation is in {reservation.status} state, must be RESERVED to provision",
        )

    reservation.callback_url = str(body.callbackURL)

    correlation_id = f"urn:uuid:{uuid4()}"
    provision_future = store.register_pending(correlation_id)

    header = NsiHeader(
        requester_nsa=reservation.requester_nsa,
        provider_nsa=reservation.provider_nsa,
        reply_to=f"{settings.base_url}{NSI_CALLBACK_PATH}",
        correlation_id=correlation_id,
    )
    soap_bytes = build_provision(header, connectionId)
    log.debug("Outbound SOAP provision request", xml=soap_bytes.decode())

    try:
        response = await nsi_client.post(settings.provider_url, content=soap_bytes, headers=_SOAP_HEADERS)
        response.raise_for_status()
    except Exception as exc:
        store.cancel_pending(correlation_id)
        log.error("Failed to send provision request to aggregator", error=str(exc))
        raise HTTPException(status_code=502, detail="Failed to reach NSI aggregator") from exc

    log.debug("Inbound SOAP provision response", xml=response.text)

    sync_msg = parse(response.content)
    if not isinstance(sync_msg, Acknowledgment):
        store.cancel_pending(correlation_id)
        raise HTTPException(
            status_code=502,
            detail=f"Unexpected sync response from aggregator: {type(sync_msg).__name__}",
        )

    store.update_status(connectionId, ReservationStatus.ACTIVATING)

    asyncio.create_task(
        _complete_provision(connectionId, provision_future, callback_client, store),
        name=f"provision-{connectionId}",
    )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=AcceptedResponse(type=ACCEPTED_TYPE, instance=f"/reservations/{connectionId}").model_dump(),
    )


async def _complete_release(
    connection_id: str,
    release_future: asyncio.Future[NsiMessage],
    callback_client: httpx.AsyncClient,
    store: ReservationStore,
) -> None:
    """Background task: drive the reservation from DEACTIVATING to RESERVED or FAILED.

    Phase 1 — wait for ReleaseConfirmed (timeout: nsi_timeout).
    Phase 2 — loop waiting for DataPlaneStateChange(active=False) (timeout: dataplane_timeout).
    """
    log = logger.bind(connection_id=connection_id)

    async def fail(reason: str) -> None:
        store.update_status(connection_id, ReservationStatus.FAILED)
        reservation = store.get(connection_id)
        if reservation is not None:
            await _send_callback(callback_client, reservation.callback_url, reservation)
        log.info("Release failed", reason=reason)

    try:
        # --- Phase 1: wait for releaseConfirmed ---
        try:
            msg = await asyncio.wait_for(release_future, timeout=settings.nsi_timeout)
        except asyncio.TimeoutError:
            await fail("no releaseConfirmed received within timeout")
            return

        if not isinstance(msg, ReleaseConfirmed):
            await fail(f"unexpected message: {type(msg).__name__}")
            return

        log.info("Release confirmed, waiting for dataPlaneStateChange")

        # --- Phase 2: wait for DataPlaneStateChange(active=False) ---
        remaining = float(settings.dataplane_timeout)
        while remaining > 0:
            dp_future = store.register_pending_by_connection(connection_id)
            start = asyncio.get_event_loop().time()
            try:
                dp_msg = await asyncio.wait_for(dp_future, timeout=remaining)
            except asyncio.TimeoutError:
                store.cancel_pending_by_connection(connection_id)
                await fail("no DataPlaneStateChange(active=False) received within timeout")
                return

            elapsed = asyncio.get_event_loop().time() - start
            remaining -= elapsed

            if not isinstance(dp_msg, DataPlaneStateChange):
                log.warning("Unexpected message while waiting for dataPlaneStateChange", msg_type=type(dp_msg).__name__)
                continue

            if not dp_msg.active:
                store.update_status(connection_id, ReservationStatus.RESERVED)
                reservation = store.get(connection_id)
                if reservation is not None:
                    await _send_callback(callback_client, reservation.callback_url, reservation)
                log.info("Data plane deactivated, state is RESERVED")
                return

            log.info("DataPlaneStateChange active=True, continuing to wait")

        await fail("no DataPlaneStateChange(active=False) received within timeout")

    except Exception:
        log.exception("Unexpected error in _complete_release")
        with contextlib.suppress(Exception):
            await fail("internal error")


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
    callback_client: httpx.AsyncClient = Depends(get_callback_client),
    store: ReservationStore = Depends(get_reservation_store),
) -> JSONResponse:
    """Release the connection identified by ``connectionId``.

    Only allowed when the reservation is in the ``ACTIVATED`` state.
    On acceptance it transitions to ``DEACTIVATING``.  The final result
    (``RESERVED`` or ``FAILED``) is delivered to ``callbackURL``.
    """
    log = logger.bind(connection_id=connectionId, callback_url=str(body.callbackURL))
    log.info("Release request received")
    log.debug("JSON request body", json=body.model_dump(mode="json"))

    reservation = store.get(connectionId)
    if reservation is None:
        raise HTTPException(status_code=404, detail=f"Reservation {connectionId!r} not found")
    if reservation.status != ReservationStatus.ACTIVATED:
        raise HTTPException(
            status_code=409,
            detail=f"Reservation is in {reservation.status} state, must be ACTIVATED to release",
        )

    reservation.callback_url = str(body.callbackURL)

    correlation_id = f"urn:uuid:{uuid4()}"
    release_future = store.register_pending(correlation_id)

    header = NsiHeader(
        requester_nsa=reservation.requester_nsa,
        provider_nsa=reservation.provider_nsa,
        reply_to=f"{settings.base_url}{NSI_CALLBACK_PATH}",
        correlation_id=correlation_id,
    )
    soap_bytes = build_release(header, connectionId)
    log.debug("Outbound SOAP release request", xml=soap_bytes.decode())

    try:
        response = await nsi_client.post(settings.provider_url, content=soap_bytes, headers=_SOAP_HEADERS)
        response.raise_for_status()
    except Exception as exc:
        store.cancel_pending(correlation_id)
        log.error("Failed to send release request to aggregator", error=str(exc))
        raise HTTPException(status_code=502, detail="Failed to reach NSI aggregator") from exc

    log.debug("Inbound SOAP release response", xml=response.text)

    sync_msg = parse(response.content)
    if not isinstance(sync_msg, Acknowledgment):
        store.cancel_pending(correlation_id)
        raise HTTPException(
            status_code=502,
            detail=f"Unexpected sync response from aggregator: {type(sync_msg).__name__}",
        )

    store.update_status(connectionId, ReservationStatus.DEACTIVATING)

    asyncio.create_task(
        _complete_release(connectionId, release_future, callback_client, store),
        name=f"release-{connectionId}",
    )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=AcceptedResponse(type=ACCEPTED_TYPE, instance=f"/reservations/{connectionId}").model_dump(),
    )


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
    logger.debug("JSON request body", json=body.model_dump(mode="json"))

    # TODO: validate state, send NSI terminate request to aggregator

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=AcceptedResponse(type=ACCEPTED_TYPE, instance=f"/reservations/{connectionId}").model_dump(),
    )


@router.get(
    "/{connectionId}",
    response_model=ReservationDetail,
    summary="Get reservation details",
)
async def get_reservation(
    connectionId: str,
    store: ReservationStore = Depends(get_reservation_store),
) -> ReservationDetail:
    """Return the details of the reservation identified by ``connectionId``."""
    logger.info("Get reservation", connection_id=connectionId)
    reservation = store.get(connectionId)
    if reservation is None:
        raise HTTPException(status_code=404, detail=f"Reservation {connectionId!r} not found")
    return ReservationDetail(
        globalReservationId=reservation.global_reservation_id,
        connectionId=reservation.connection_id,
        description=reservation.description,
        criteria=reservation.criteria,
        status=reservation.status,
    )


@router.get(
    "",
    response_model=ReservationsListResponse,
    summary="List all reservations",
)
async def list_reservations(
    store: ReservationStore = Depends(get_reservation_store),
) -> ReservationsListResponse:
    """Return a list of all reservations and their details."""
    logger.info("List all reservations")
    return ReservationsListResponse(
        reservations=[
            ReservationDetail(
                globalReservationId=r.global_reservation_id,
                connectionId=r.connection_id,
                description=r.description,
                criteria=r.criteria,
                status=r.status,
            )
            for r in store.get_all()
        ]
    )
