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


@pytest.mark.parametrize(
    ("states", "has_error_event", "expected"),
    [
        pytest.param(
            ConnectionStates("ReserveStart", "Provisioned", "Created", True),
            True,
            ReservationStatus.FAILED,
            id="error-with-normal-states",
        ),
        pytest.param(
            ConnectionStates("ReserveStart", "Released", "Failed", False),
            True,
            ReservationStatus.FAILED,
            id="error-with-failed-lifecycle",
        ),
        pytest.param(
            ConnectionStates("ReserveStart", "Released", "Terminated", False),
            True,
            ReservationStatus.TERMINATED,
            id="error-does-not-override-terminated",
        ),
        pytest.param(
            ConnectionStates("ReserveStart", "Provisioned", "Created", True),
            False,
            ReservationStatus.ACTIVATED,
            id="no-error-normal-result",
        ),
    ],
)
def test_error_event_flag(states: ConnectionStates, has_error_event: bool, expected: ReservationStatus) -> None:
    assert map_nsi_states_to_status(states, has_error_event=has_error_event) == expected
