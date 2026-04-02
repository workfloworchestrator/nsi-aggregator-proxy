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


"""Reservation API endpoints."""

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import uuid4

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from aggregator_proxy.dependencies import get_callback_client, get_nsi_client, get_reservation_store
from aggregator_proxy.models import (
    P2PS,
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
    ErrorEvent,
    NsiHeader,
    NsiMessage,
    ProvisionConfirmed,
    QueryReservation,
    ReleaseConfirmed,
    ReserveCommitConfirmed,
    ReserveCommitFailed,
    ReserveConfirmed,
    ReserveFailed,
    ReserveResponse,
    ReserveTimeout,
    ServiceException,
    TerminateConfirmed,
    Variable,
    build_provision,
    build_query_notification_sync,
    build_query_summary_sync,
    build_release,
    build_reserve,
    build_reserve_commit,
    build_terminate,
    parse,
    parse_query_notification_sync,
    parse_query_summary_sync,
)
from aggregator_proxy.reservation_store import Reservation, ReservationStore
from aggregator_proxy.routers.nsi_callback import NSI_CALLBACK_PATH
from aggregator_proxy.settings import settings
from aggregator_proxy.state_mapping import map_nsi_states_to_status

logger = structlog.get_logger(__name__)

NsiClient = Annotated[httpx.AsyncClient, Depends(get_nsi_client)]
CallbackClient = Annotated[httpx.AsyncClient, Depends(get_callback_client)]
Store = Annotated[ReservationStore, Depends(get_reservation_store)]

ACCEPTED_TYPE = "https://github.com/workfloworchestrator/nsi-aggregator-proxy#202-accepted"
_SOAP_ACTION_BASE = "http://schemas.ogf.org/nsi/2013/12/connection/service"


def _soap_headers(operation: str) -> dict[str, str]:
    """Return SOAP HTTP headers with the correct SOAPAction for the given NSI operation."""
    return {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"{_SOAP_ACTION_BASE}/{operation}"'}


def _raise_for_status(response: httpx.Response, operation: str, **extra: object) -> None:
    """Log response body on error, then raise."""
    if not response.is_success:
        logger.error(
            "Aggregator request failed",
            operation=operation,
            status_code=response.status_code,
            response_body=response.text,
            **extra,
        )
    response.raise_for_status()


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
        lastError=reservation.last_error,
    )
    payload = detail.model_dump()
    logger.info(
        "Delivering callback",
        connection_id=reservation.connection_id,
        status=reservation.status,
        callback_url=callback_url,
    )
    logger.debug("Outbound JSON callback", callback_url=callback_url, json=payload)
    try:
        await callback_client.post(callback_url, json=payload)
    except Exception as exc:
        logger.error("Failed to deliver callback", callback_url=callback_url, error=str(exc))


def _query_header() -> NsiHeader:
    """Build an NsiHeader for querySummarySync requests."""
    return NsiHeader(
        requester_nsa=settings.requester_nsa,
        provider_nsa=settings.provider_nsa,
        reply_to=f"{settings.base_url}{NSI_CALLBACK_PATH}",
    )


def _format_variables(variables: list[Variable] | None, indent: str = "  ") -> list[str]:
    """Format a list of ServiceException variables as indented key=value strings."""
    if not variables:
        return []
    return [f"{indent}{var.type}={var.value}" for var in variables]


def _format_service_exception(exc: ServiceException) -> str:
    """Format a ServiceException into a human-readable string.

    When the exception has child exceptions (e.g. from downstream NSAs),
    the child details are appended as they typically contain the actual error.
    """
    parts = [f"[{exc.error_id}] {exc.text} (nsaId={exc.nsa_id})"]
    parts.extend(_format_variables(exc.variables))
    if exc.child_exceptions:
        for child in exc.child_exceptions:
            parts.append(f"  child [{child.error_id}] {child.text} (nsaId={child.nsa_id})")
            parts.extend(_format_variables(child.variables, indent="    "))
    return "\n".join(parts)


def _format_last_error(error_events: list[ErrorEvent]) -> str | None:
    """Return a human-readable error string from the most recent error event."""
    if not error_events:
        return None
    latest = max(error_events, key=lambda e: e.notification_id)
    if latest.service_exception is not None:
        return f"{latest.event}: {latest.service_exception.error_id}: {latest.service_exception.text}"
    return latest.event


def _update_store_from_query(
    store: ReservationStore, qr: QueryReservation, error_events: list[ErrorEvent] | None = None
) -> Reservation:
    """Create or update a Reservation in the store from a QueryReservation."""
    has_errors = bool(error_events)
    mapped_status = map_nsi_states_to_status(qr.connection_states, has_error_event=has_errors)
    existing = store.get(qr.connection_id)

    criteria: CriteriaResponse | None = None
    if qr.capacity is not None and qr.source_stp is not None and qr.dest_stp is not None:
        criteria = CriteriaResponse(
            version=qr.criteria_version or 1,
            serviceType=qr.service_type,
            p2ps=P2PS(capacity=qr.capacity, sourceSTP=qr.source_stp, destSTP=qr.dest_stp),
        )

    last_error = _format_last_error(error_events or [])

    if existing is not None:
        existing.status = mapped_status
        if last_error is not None:
            existing.last_error = last_error
        if criteria is not None:
            existing.criteria = criteria
        reservation = existing
    else:
        reservation = Reservation(
            connection_id=qr.connection_id,
            status=mapped_status,
            global_reservation_id=qr.global_reservation_id,
            description=qr.description,
            criteria=criteria,
            requester_nsa=qr.requester_nsa,
            last_error=last_error,
        )
        store.create(reservation)

    if error_events:
        seen_ids = reservation.seen_error_notification_ids
        new_events = [e for e in error_events if seen_ids is None or e.notification_id not in seen_ids]
        if new_events:
            logger.info(
                "New error events detected for reservation",
                connection_id=qr.connection_id,
                error_event_count=len(new_events),
                events=[e.event for e in new_events],
            )
        else:
            logger.debug(
                "Error events detected for reservation (already seen)",
                connection_id=qr.connection_id,
                error_event_count=len(error_events),
            )
        if reservation.seen_error_notification_ids is None:
            reservation.seen_error_notification_ids = set()
        reservation.seen_error_notification_ids.update(e.notification_id for e in error_events)

    return reservation


async def _query_error_events(
    nsi_client: httpx.AsyncClient,
    connection_id: str,
) -> list[ErrorEvent]:
    """Query the aggregator for error event notifications for a connection."""
    header = _query_header()
    soap_bytes = build_query_notification_sync(header, connection_id)
    logger.debug("Outbound SOAP queryNotificationSync request", xml=soap_bytes.decode(), connection_id=connection_id)
    try:
        response = await nsi_client.post(
            settings.provider_url, content=soap_bytes, headers=_soap_headers("queryNotificationSync")
        )
        _raise_for_status(response, "queryNotificationSync", connection_id=connection_id)
    except Exception as exc:
        logger.error(
            "Failed to query notifications from aggregator",
            connection_id=connection_id,
            error=str(exc),
        )
        return []
    logger.debug("Inbound SOAP queryNotificationSyncConfirmed response", xml=response.text, connection_id=connection_id)
    try:
        events = parse_query_notification_sync(response.content)
    except Exception as exc:
        logger.error(
            "Failed to parse queryNotificationSync response",
            connection_id=connection_id,
            error=str(exc),
        )
        return []
    return events


async def _refresh_reservation(
    connection_id: str,
    nsi_client: httpx.AsyncClient,
    store: ReservationStore,
) -> Reservation | None:
    """Query the aggregator for a single reservation and update the store."""
    header = _query_header()
    soap_bytes = build_query_summary_sync(header, connection_id=connection_id)
    logger.debug("Outbound SOAP querySummarySync request", xml=soap_bytes.decode(), connection_id=connection_id)
    try:
        response = await nsi_client.post(
            settings.provider_url, content=soap_bytes, headers=_soap_headers("querySummarySync")
        )
        _raise_for_status(response, "querySummarySync", connection_id=connection_id)
    except Exception as exc:
        logger.error("Failed to refresh reservation from aggregator", connection_id=connection_id, error=str(exc))
        raise HTTPException(status_code=502, detail="Failed to reach NSI aggregator") from exc
    logger.debug("Inbound SOAP querySummarySyncConfirmed response", xml=response.text, connection_id=connection_id)
    reservations = parse_query_summary_sync(response.content)
    if not reservations:
        logger.info("Reservation not found on aggregator", connection_id=connection_id)
        return None
    error_events = await _query_error_events(nsi_client, connection_id)
    reservation = _update_store_from_query(store, reservations[0], error_events)
    logger.info("Reservation state refreshed from aggregator", connection_id=connection_id, status=reservation.status)
    return reservation


async def _refresh_all_reservations(
    nsi_client: httpx.AsyncClient,
    store: ReservationStore,
) -> None:
    """Query the aggregator for all reservations and update the store."""
    logger.info("Refreshing all reservations from aggregator")
    header = _query_header()
    soap_bytes = build_query_summary_sync(header)
    logger.debug("Outbound SOAP querySummarySync (all) request", xml=soap_bytes.decode())
    try:
        response = await nsi_client.post(
            settings.provider_url, content=soap_bytes, headers=_soap_headers("querySummarySync")
        )
        _raise_for_status(response, "querySummarySync")
    except Exception as exc:
        logger.error("Failed to refresh all reservations from aggregator", error=str(exc))
        raise HTTPException(status_code=502, detail="Failed to reach NSI aggregator") from exc
    logger.debug("Inbound SOAP querySummarySyncConfirmed (all) response", xml=response.text)
    reservations = parse_query_summary_sync(response.content)
    logger.info("Aggregator returned reservations", count=len(reservations))

    # For reservations not already in a terminal/failed state, query error events concurrently
    needs_notification: list[QueryReservation] = []
    terminal_reservations: list[QueryReservation] = []
    for qr in reservations:
        preliminary_status = map_nsi_states_to_status(qr.connection_states)
        if preliminary_status in (ReservationStatus.TERMINATED, ReservationStatus.FAILED):
            terminal_reservations.append(qr)
        else:
            needs_notification.append(qr)

    for qr in terminal_reservations:
        _update_store_from_query(store, qr)

    if needs_notification:
        logger.debug("Checking error events for active reservations", count=len(needs_notification))
        error_results = await asyncio.gather(
            *(_query_error_events(nsi_client, qr.connection_id) for qr in needs_notification)
        )
        for qr, error_events in zip(needs_notification, error_results, strict=True):
            _update_store_from_query(store, qr, error_events)


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
            reservation.last_error = reason
            await _send_callback(callback_client, reservation.callback_url, reservation)
        log.info("Reservation failed", reason=reason)

    try:
        # --- Phase 1: wait for reserveConfirmed / failure ---
        try:
            msg = await asyncio.wait_for(reserve_future, timeout=settings.nsi_timeout)
        except asyncio.TimeoutError:
            await fail("no reserveConfirmed received within timeout")
            return

        match msg:
            case ReserveFailed():
                await fail(f"reserveFailed: {_format_service_exception(msg.service_exception)}")
                return
            case ReserveTimeout():
                await fail("reserveTimeout from aggregator")
                return
            case ReserveConfirmed():
                store.update_criteria(connection_id, msg)
                log.info("Reserve confirmed by aggregator")
            case _:
                await fail(f"unexpected message: {type(msg).__name__}")
                return

        # --- Phase 2: send reserveCommit, wait for commit result ---
        commit_correlation_id = f"urn:uuid:{uuid4()}"
        commit_future = store.register_pending(commit_correlation_id)
        reservation = store.get(connection_id)
        requester_nsa = reservation.requester_nsa if reservation is not None else ""
        header = NsiHeader(
            requester_nsa=requester_nsa,
            provider_nsa=settings.provider_nsa,
            reply_to=f"{settings.base_url}{NSI_CALLBACK_PATH}",
            correlation_id=commit_correlation_id,
        )
        soap_bytes = build_reserve_commit(header, connection_id)
        log.debug("Outbound SOAP reserveCommit request", xml=soap_bytes.decode())
        log.info("Sending reserveCommit to aggregator")
        try:
            response = await nsi_client.post(
                settings.provider_url, content=soap_bytes, headers=_soap_headers("reserveCommit")
            )
            _raise_for_status(response, "reserveCommit", connection_id=connection_id)
        except Exception as exc:
            store.cancel_pending(commit_correlation_id)
            log.error("Failed to send reserveCommit to aggregator", error=str(exc))
            await fail("failed to send reserveCommit to aggregator")
            return

        log.debug("Inbound SOAP reserveCommit response", xml=response.text)
        log.info("ReserveCommit accepted by aggregator, waiting for commit confirmation")

        try:
            commit_msg = await asyncio.wait_for(commit_future, timeout=settings.nsi_timeout)
        except asyncio.TimeoutError:
            await fail("no reserveCommitConfirmed received within timeout")
            return

        match commit_msg:
            case ReserveCommitFailed():
                await fail(f"reserveCommitFailed: {_format_service_exception(commit_msg.service_exception)}")
                return
            case ReserveCommitConfirmed():
                pass
            case _:
                await fail(f"unexpected message: {type(commit_msg).__name__}")
                return
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
    nsi_client: NsiClient,
    callback_client: CallbackClient,
    store: Store,
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

    if body.providerNSA != settings.provider_nsa:
        raise HTTPException(
            status_code=400,
            detail=(
                f"providerNSA {body.providerNSA!r} does not match the configured provider {settings.provider_nsa!r}"
            ),
        )

    correlation_id = f"urn:uuid:{uuid4()}"
    # Register the future BEFORE sending the SOAP request so the callback
    # can never arrive before we are ready to receive it.
    reserve_future = store.register_pending(correlation_id)

    now = datetime.now(timezone.utc)
    header = NsiHeader(
        requester_nsa=body.requesterNSA,
        provider_nsa=settings.provider_nsa,
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
    log.info("Sending reserve request to aggregator")

    try:
        response = await nsi_client.post(settings.provider_url, content=soap_bytes, headers=_soap_headers("reserve"))
        _raise_for_status(response, "reserve")
    except Exception as exc:
        store.cancel_pending(correlation_id)
        logger.error("Failed to send reserve request to aggregator", error=str(exc))
        raise HTTPException(status_code=502, detail="Failed to reach NSI aggregator") from exc

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
            provider_nsa=settings.provider_nsa,
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
            reservation.last_error = reason
            await _send_callback(callback_client, reservation.callback_url, reservation)
        log.info("Provision failed", reason=reason)

    try:
        # --- Phase 1: wait for provisionConfirmed ---
        try:
            msg = await asyncio.wait_for(provision_future, timeout=settings.nsi_timeout)
        except asyncio.TimeoutError:
            await fail("no provisionConfirmed received within timeout")
            return

        match msg:
            case ProvisionConfirmed():
                log.info("Provision confirmed by aggregator, waiting for data plane activation")
            case _:
                await fail(f"unexpected message: {type(msg).__name__}")
                return

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

            match dp_msg:
                case DataPlaneStateChange(active=True):
                    store.update_status(connection_id, ReservationStatus.ACTIVATED)
                    reservation = store.get(connection_id)
                    if reservation is not None:
                        await _send_callback(callback_client, reservation.callback_url, reservation)
                    log.info("Data plane active, state is ACTIVATED")
                    return
                case DataPlaneStateChange(active=False):
                    log.debug("DataPlaneStateChange active=False, continuing to wait")
                case _:
                    log.warning(
                        "Unexpected message while waiting for dataPlaneStateChange",
                        msg_type=type(dp_msg).__name__,
                    )

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
    nsi_client: NsiClient,
    callback_client: CallbackClient,
    store: Store,
) -> JSONResponse:
    """Provision the connection identified by ``connectionId``.

    Only allowed when the reservation is in the ``RESERVED`` state.
    On acceptance it transitions to ``ACTIVATING``.  The final result
    (``ACTIVATED`` or ``FAILED``) is delivered to ``callbackURL``.
    """
    log = logger.bind(connection_id=connectionId, callback_url=str(body.callbackURL))
    log.info("Provision request received")
    log.debug("JSON request body", json=body.model_dump(mode="json"))

    reservation = await _refresh_reservation(connectionId, nsi_client, store)
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
        provider_nsa=settings.provider_nsa,
        reply_to=f"{settings.base_url}{NSI_CALLBACK_PATH}",
        correlation_id=correlation_id,
    )
    soap_bytes = build_provision(header, connectionId)
    log.debug("Outbound SOAP provision request", xml=soap_bytes.decode())
    log.info("Sending provision request to aggregator")

    try:
        response = await nsi_client.post(settings.provider_url, content=soap_bytes, headers=_soap_headers("provision"))
        _raise_for_status(response, "provision", connection_id=connectionId)
    except Exception as exc:
        store.cancel_pending(correlation_id)
        log.error("Failed to send provision request to aggregator", error=str(exc))
        raise HTTPException(status_code=502, detail="Failed to reach NSI aggregator") from exc

    log.debug("Inbound SOAP provision response", xml=response.text)
    log.info("Provision accepted by aggregator, waiting for confirmation")

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
            reservation.last_error = reason
            await _send_callback(callback_client, reservation.callback_url, reservation)
        log.info("Release failed", reason=reason)

    try:
        # --- Phase 1: wait for releaseConfirmed ---
        try:
            msg = await asyncio.wait_for(release_future, timeout=settings.nsi_timeout)
        except asyncio.TimeoutError:
            await fail("no releaseConfirmed received within timeout")
            return

        match msg:
            case ReleaseConfirmed():
                log.info("Release confirmed by aggregator, waiting for data plane deactivation")
            case _:
                await fail(f"unexpected message: {type(msg).__name__}")
                return

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

            match dp_msg:
                case DataPlaneStateChange(active=False):
                    store.update_status(connection_id, ReservationStatus.RESERVED)
                    reservation = store.get(connection_id)
                    if reservation is not None:
                        await _send_callback(callback_client, reservation.callback_url, reservation)
                    log.info("Data plane deactivated, state is RESERVED")
                    return
                case DataPlaneStateChange(active=True):
                    log.debug("DataPlaneStateChange active=True, continuing to wait")
                case _:
                    log.warning(
                        "Unexpected message while waiting for dataPlaneStateChange",
                        msg_type=type(dp_msg).__name__,
                    )

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
    nsi_client: NsiClient,
    callback_client: CallbackClient,
    store: Store,
) -> JSONResponse:
    """Release the connection identified by ``connectionId``.

    Only allowed when the reservation is in the ``ACTIVATED`` state.
    On acceptance it transitions to ``DEACTIVATING``.  The final result
    (``RESERVED`` or ``FAILED``) is delivered to ``callbackURL``.
    """
    log = logger.bind(connection_id=connectionId, callback_url=str(body.callbackURL))
    log.info("Release request received")
    log.debug("JSON request body", json=body.model_dump(mode="json"))

    reservation = await _refresh_reservation(connectionId, nsi_client, store)
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
        provider_nsa=settings.provider_nsa,
        reply_to=f"{settings.base_url}{NSI_CALLBACK_PATH}",
        correlation_id=correlation_id,
    )
    soap_bytes = build_release(header, connectionId)
    log.debug("Outbound SOAP release request", xml=soap_bytes.decode())
    log.info("Sending release request to aggregator")

    try:
        response = await nsi_client.post(settings.provider_url, content=soap_bytes, headers=_soap_headers("release"))
        _raise_for_status(response, "release", connection_id=connectionId)
    except Exception as exc:
        store.cancel_pending(correlation_id)
        log.error("Failed to send release request to aggregator", error=str(exc))
        raise HTTPException(status_code=502, detail="Failed to reach NSI aggregator") from exc

    log.debug("Inbound SOAP release response", xml=response.text)
    log.info("Release accepted by aggregator, waiting for confirmation")

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


async def _complete_terminate(
    connection_id: str,
    terminate_future: asyncio.Future[NsiMessage],
    callback_client: httpx.AsyncClient,
    store: ReservationStore,
) -> None:
    """Background task: drive the reservation to TERMINATED.

    Wait for TerminateConfirmed (timeout: nsi_timeout).
    Both success and failure end in TERMINATED per the state machine.
    """
    log = logger.bind(connection_id=connection_id)

    async def terminated(reason: str) -> None:
        store.update_status(connection_id, ReservationStatus.TERMINATED)
        reservation = store.get(connection_id)
        if reservation is not None:
            await _send_callback(callback_client, reservation.callback_url, reservation)
        log.info("Terminate completed", reason=reason)

    try:
        try:
            msg = await asyncio.wait_for(terminate_future, timeout=settings.nsi_timeout)
        except asyncio.TimeoutError:
            await terminated("no terminateConfirmed received within timeout")
            return

        match msg:
            case TerminateConfirmed():
                await terminated("terminateConfirmed received")
            case _:
                await terminated(f"unexpected message: {type(msg).__name__}")
                return

    except Exception:
        log.exception("Unexpected error in _complete_terminate")
        with contextlib.suppress(Exception):
            await terminated("internal error")


@router.delete(
    "/{connectionId}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedResponse,
    summary="Terminate a connection",
)
async def terminate_reservation(
    connectionId: str,
    body: CallbackRequest,
    nsi_client: NsiClient,
    callback_client: CallbackClient,
    store: Store,
) -> JSONResponse:
    """Terminate the connection identified by ``connectionId``.

    Only allowed when the reservation is in the ``RESERVED`` or ``FAILED``
    state.  On acceptance it transitions to ``TERMINATED``.  The final result
    is delivered to ``callbackURL``.
    """
    log = logger.bind(connection_id=connectionId, callback_url=str(body.callbackURL))
    log.info("Terminate request received")
    log.debug("JSON request body", json=body.model_dump(mode="json"))

    reservation = await _refresh_reservation(connectionId, nsi_client, store)
    if reservation is None:
        raise HTTPException(status_code=404, detail=f"Reservation {connectionId!r} not found")
    if reservation.status not in (ReservationStatus.RESERVED, ReservationStatus.FAILED):
        raise HTTPException(
            status_code=409,
            detail=f"Reservation is in {reservation.status} state, must be RESERVED or FAILED to terminate",
        )

    reservation.callback_url = str(body.callbackURL)

    correlation_id = f"urn:uuid:{uuid4()}"
    terminate_future = store.register_pending(correlation_id)

    header = NsiHeader(
        requester_nsa=reservation.requester_nsa,
        provider_nsa=settings.provider_nsa,
        reply_to=f"{settings.base_url}{NSI_CALLBACK_PATH}",
        correlation_id=correlation_id,
    )
    soap_bytes = build_terminate(header, connectionId)
    log.debug("Outbound SOAP terminate request", xml=soap_bytes.decode())
    log.info("Sending terminate request to aggregator")

    try:
        response = await nsi_client.post(settings.provider_url, content=soap_bytes, headers=_soap_headers("terminate"))
        _raise_for_status(response, "terminate", connection_id=connectionId)
    except Exception as exc:
        store.cancel_pending(correlation_id)
        log.error("Failed to send terminate request to aggregator", error=str(exc))
        raise HTTPException(status_code=502, detail="Failed to reach NSI aggregator") from exc

    log.debug("Inbound SOAP terminate response", xml=response.text)
    log.info("Terminate accepted by aggregator, waiting for confirmation")

    sync_msg = parse(response.content)
    if not isinstance(sync_msg, Acknowledgment):
        store.cancel_pending(correlation_id)
        raise HTTPException(
            status_code=502,
            detail=f"Unexpected sync response from aggregator: {type(sync_msg).__name__}",
        )

    asyncio.create_task(
        _complete_terminate(connectionId, terminate_future, callback_client, store),
        name=f"terminate-{connectionId}",
    )

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
    nsi_client: NsiClient,
    store: Store,
) -> ReservationDetail:
    """Return the details of the reservation identified by ``connectionId``."""
    logger.debug("Get reservation request", connection_id=connectionId)
    reservation = await _refresh_reservation(connectionId, nsi_client, store)
    if reservation is None:
        raise HTTPException(status_code=404, detail=f"Reservation {connectionId!r} not found")
    return ReservationDetail(
        globalReservationId=reservation.global_reservation_id,
        connectionId=reservation.connection_id,
        description=reservation.description,
        criteria=reservation.criteria,
        status=reservation.status,
        lastError=reservation.last_error,
    )


@router.get(
    "",
    response_model=ReservationsListResponse,
    summary="List all reservations",
)
async def list_reservations(
    nsi_client: NsiClient,
    store: Store,
) -> ReservationsListResponse:
    """Return a list of all reservations and their details."""
    logger.debug("List all reservations request")
    await _refresh_all_reservations(nsi_client, store)
    return ReservationsListResponse(
        reservations=[
            ReservationDetail(
                globalReservationId=r.global_reservation_id,
                connectionId=r.connection_id,
                description=r.description,
                criteria=r.criteria,
                status=r.status,
                lastError=r.last_error,
            )
            for r in store.get_all()
        ]
    )
