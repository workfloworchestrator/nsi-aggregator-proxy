"""Map NSI connection sub-state machines to the simplified proxy state."""

from aggregator_proxy.models import ReservationStatus
from aggregator_proxy.nsi_soap.parser import ConnectionStates


def map_nsi_states_to_status(states: ConnectionStates, *, has_error_event: bool = False) -> ReservationStatus:
    """Derive the proxy ReservationStatus from NSI sub-state machines.

    Priority order:
    1. Terminated / PassedEndTime lifecycle → TERMINATED
    2. Failed lifecycle → FAILED
    3. ReserveTimeout / ReserveFailed / ReserveAborting → FAILED
    4. has_error_event → FAILED
    5. ReserveChecking / ReserveHeld / ReserveCommitting → RESERVING
    6. Released + active → DEACTIVATING
    7. dataPlaneStatus active → ACTIVATED
    8. Provisioned (active=false) → ACTIVATING
    9. Otherwise → RESERVED
    """
    if states.lifecycle_state in ("Terminated", "PassedEndTime"):
        return ReservationStatus.TERMINATED
    if states.lifecycle_state == "Failed":
        return ReservationStatus.FAILED
    if states.reservation_state in ("ReserveTimeout", "ReserveFailed", "ReserveAborting"):
        return ReservationStatus.FAILED
    if has_error_event:
        return ReservationStatus.FAILED
    if states.reservation_state in ("ReserveChecking", "ReserveHeld", "ReserveCommitting"):
        return ReservationStatus.RESERVING
    if states.data_plane_active and states.provision_state == "Released":
        return ReservationStatus.DEACTIVATING
    if states.data_plane_active:
        return ReservationStatus.ACTIVATED
    if states.provision_state == "Provisioned":
        return ReservationStatus.ACTIVATING
    return ReservationStatus.RESERVED
