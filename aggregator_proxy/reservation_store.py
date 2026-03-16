"""In-memory store for reservations and pending NSI correlation tracking."""

import asyncio
from dataclasses import dataclass

from aggregator_proxy.models import P2PS, CriteriaResponse, ReservationStatus
from aggregator_proxy.nsi_soap.parser import NsiMessage, ReserveConfirmed


@dataclass
class Reservation:
    """In-memory representation of a single NSI reservation."""

    connection_id: str
    status: ReservationStatus
    global_reservation_id: str | None
    description: str
    criteria: CriteriaResponse
    requester_nsa: str
    provider_nsa: str
    callback_url: str


class ReservationStore:
    """In-memory store for reservations and pending NSI correlation futures.

    Pending correlations map a correlationId to an asyncio.Future that is
    resolved when the matching async callback arrives on the NSI callback
    endpoint.  Reserving, committing, provisioning, releasing and terminating
    each register their own future so the background tasks can await them.
    """

    def __init__(self) -> None:
        """Initialise empty stores."""
        self._reservations: dict[str, Reservation] = {}
        self._pending: dict[str, asyncio.Future[NsiMessage]] = {}
        self._pending_by_connection: dict[str, asyncio.Future[NsiMessage]] = {}

    # ------------------------------------------------------------------
    # Reservation CRUD
    # ------------------------------------------------------------------

    def create(self, reservation: Reservation) -> None:
        """Store a new reservation."""
        self._reservations[reservation.connection_id] = reservation

    def get(self, connection_id: str) -> Reservation | None:
        """Return the reservation for the given connectionId, or None."""
        return self._reservations.get(connection_id)

    def get_all(self) -> list[Reservation]:
        """Return all stored reservations."""
        return list(self._reservations.values())

    def update_status(self, connection_id: str, status: ReservationStatus) -> None:
        """Update the status of an existing reservation."""
        self._reservations[connection_id].status = status

    def update_criteria(self, connection_id: str, msg: ReserveConfirmed) -> None:
        """Replace stored criteria with the confirmed values from the aggregator."""
        reservation = self._reservations[connection_id]
        reservation.criteria = CriteriaResponse(
            version=msg.criteria_version,
            serviceType=msg.service_type,
            p2ps=P2PS(
                capacity=msg.capacity,
                sourceSTP=msg.source_stp,
                destSTP=msg.dest_stp,
            ),
        )

    # ------------------------------------------------------------------
    # Pending correlation tracking
    # ------------------------------------------------------------------

    def register_pending(self, correlation_id: str) -> asyncio.Future[NsiMessage]:
        """Create and register a Future for the given correlationId.

        Must be called from within an async context (the running event loop
        is used to create the Future).
        """
        future: asyncio.Future[NsiMessage] = asyncio.get_running_loop().create_future()
        self._pending[correlation_id] = future
        return future

    def resolve_pending(self, correlation_id: str, message: NsiMessage) -> bool:
        """Set the result on a pending Future; returns True if one was found."""
        future = self._pending.pop(correlation_id, None)
        if future is None or future.done():
            return False
        future.set_result(message)
        return True

    def cancel_pending(self, correlation_id: str) -> None:
        """Cancel and remove a pending Future (cleanup on send failure)."""
        future = self._pending.pop(correlation_id, None)
        if future is not None and not future.done():
            future.cancel()

    # ------------------------------------------------------------------
    # Pending connection-based tracking (for notifications like
    # DataPlaneStateChange where the correlationId is aggregator-generated)
    # ------------------------------------------------------------------

    def register_pending_by_connection(self, connection_id: str) -> asyncio.Future[NsiMessage]:
        """Create and register a Future keyed by connectionId."""
        future: asyncio.Future[NsiMessage] = asyncio.get_running_loop().create_future()
        self._pending_by_connection[connection_id] = future
        return future

    def resolve_pending_by_connection(self, connection_id: str, message: NsiMessage) -> bool:
        """Set the result on a connection-keyed Future; returns True if one was found."""
        future = self._pending_by_connection.pop(connection_id, None)
        if future is None or future.done():
            return False
        future.set_result(message)
        return True

    def cancel_pending_by_connection(self, connection_id: str) -> None:
        """Cancel and remove a connection-keyed pending Future."""
        future = self._pending_by_connection.pop(connection_id, None)
        if future is not None and not future.done():
            future.cancel()
