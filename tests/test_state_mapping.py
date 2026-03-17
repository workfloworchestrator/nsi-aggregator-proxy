"""Tests for NSI state to proxy status mapping."""

import pytest

from aggregator_proxy.models import ReservationStatus
from aggregator_proxy.nsi_soap.parser import ConnectionStates
from aggregator_proxy.state_mapping import map_nsi_states_to_status


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        # Terminated lifecycle
        (
            ConnectionStates("ReserveStart", "Released", "Terminated", False),
            ReservationStatus.TERMINATED,
        ),
        # PassedEndTime lifecycle
        (
            ConnectionStates("ReserveStart", "Released", "PassedEndTime", False),
            ReservationStatus.TERMINATED,
        ),
        # Terminated lifecycle takes precedence even when data plane is active
        (
            ConnectionStates("ReserveStart", "Provisioned", "Terminated", True),
            ReservationStatus.TERMINATED,
        ),
        # Failed lifecycle
        (
            ConnectionStates("ReserveStart", "Released", "Failed", False),
            ReservationStatus.FAILED,
        ),
        # ReserveTimeout → FAILED
        (
            ConnectionStates("ReserveTimeout", "Released", "Created", False),
            ReservationStatus.FAILED,
        ),
        # ReserveFailed → FAILED
        (
            ConnectionStates("ReserveFailed", "Released", "Created", False),
            ReservationStatus.FAILED,
        ),
        # ReserveAborting → FAILED
        (
            ConnectionStates("ReserveAborting", "Released", "Created", False),
            ReservationStatus.FAILED,
        ),
        # ReserveChecking → RESERVING
        (
            ConnectionStates("ReserveChecking", "Released", "Created", False),
            ReservationStatus.RESERVING,
        ),
        # ReserveHeld → RESERVING
        (
            ConnectionStates("ReserveHeld", "Released", "Created", False),
            ReservationStatus.RESERVING,
        ),
        # ReserveCommitting → RESERVING
        (
            ConnectionStates("ReserveCommitting", "Released", "Created", False),
            ReservationStatus.RESERVING,
        ),
        # Released + data plane active → DEACTIVATING
        (
            ConnectionStates("ReserveStart", "Released", "Created", True),
            ReservationStatus.DEACTIVATING,
        ),
        # Provisioned + data plane active → ACTIVATED
        (
            ConnectionStates("ReserveStart", "Provisioned", "Created", True),
            ReservationStatus.ACTIVATED,
        ),
        # Provisioned but not active → ACTIVATING
        (
            ConnectionStates("ReserveStart", "Provisioned", "Created", False),
            ReservationStatus.ACTIVATING,
        ),
        # Default: ReserveStart + Released + Created → RESERVED
        (
            ConnectionStates("ReserveStart", "Released", "Created", False),
            ReservationStatus.RESERVED,
        ),
    ],
    ids=[
        "terminated",
        "passed-end-time",
        "terminated-overrides-active",
        "failed-lifecycle",
        "reserve-timeout",
        "reserve-failed",
        "reserve-aborting",
        "reserve-checking",
        "reserve-held",
        "reserve-committing",
        "released-active-deactivating",
        "data-plane-active",
        "provisioned-not-active",
        "default-reserved",
    ],
)
def test_map_nsi_states_to_status(states: ConnectionStates, expected: ReservationStatus) -> None:
    assert map_nsi_states_to_status(states) == expected


class TestErrorEventFlag:
    """Tests for has_error_event parameter."""

    def test_error_event_with_normal_states_yields_failed(self) -> None:
        states = ConnectionStates("ReserveStart", "Provisioned", "Created", True)
        assert map_nsi_states_to_status(states, has_error_event=True) == ReservationStatus.FAILED

    def test_error_event_does_not_override_terminated(self) -> None:
        states = ConnectionStates("ReserveStart", "Released", "Terminated", False)
        assert map_nsi_states_to_status(states, has_error_event=True) == ReservationStatus.TERMINATED

    def test_error_event_with_failed_lifecycle_stays_failed(self) -> None:
        states = ConnectionStates("ReserveStart", "Released", "Failed", False)
        assert map_nsi_states_to_status(states, has_error_event=True) == ReservationStatus.FAILED

    def test_error_event_false_does_not_affect_result(self) -> None:
        states = ConnectionStates("ReserveStart", "Provisioned", "Created", True)
        assert map_nsi_states_to_status(states, has_error_event=False) == ReservationStatus.ACTIVATED
