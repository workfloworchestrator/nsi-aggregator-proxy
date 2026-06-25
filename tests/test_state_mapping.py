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

from datetime import datetime, timedelta, timezone

import pytest

from aggregator_proxy.models import ReservationStatus
from aggregator_proxy.nsi_soap.parser import ConnectionStates, DataPlaneStateChange, OperationResult
from aggregator_proxy.state_mapping import derive_status, map_nsi_states_to_status


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


# ---------------------------------------------------------------------------
# Time-aware derive_status
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_TIMEOUT = timedelta(seconds=300)


def _iso(offset_seconds: int) -> str:
    return (_NOW + timedelta(seconds=offset_seconds)).isoformat()


def _result(operation: str, offset_seconds: int, result_id: int = 1) -> OperationResult:
    return OperationResult(result_id=result_id, timestamp=_iso(offset_seconds), operation=operation, connection_id="c")


def _dp(active: bool, offset_seconds: int) -> DataPlaneStateChange:
    return DataPlaneStateChange(
        connection_id="c",
        notification_id=1,
        timestamp=_iso(offset_seconds),
        active=active,
        version=1,
        version_consistent=True,
    )


@pytest.mark.parametrize(
    ("base", "results", "data_plane_changes", "start_time", "expected_status", "expect_reason"),
    [
        pytest.param(ReservationStatus.RESERVED, [], [], None, ReservationStatus.RESERVED, False, id="stable-reserved"),
        pytest.param(
            ReservationStatus.ACTIVATED, [], [], None, ReservationStatus.ACTIVATED, False, id="stable-activated"
        ),
        pytest.param(
            ReservationStatus.ACTIVATING,
            [_result("provisionConfirmed", -10)],
            [],
            None,
            ReservationStatus.ACTIVATING,
            False,
            id="activating-within-grace",
        ),
        pytest.param(
            ReservationStatus.ACTIVATING,
            [_result("provisionConfirmed", -600)],
            [],
            None,
            ReservationStatus.FAILED,
            True,
            id="activating-never-active-past-timeout",
        ),
        pytest.param(
            ReservationStatus.ACTIVATING,
            [_result("provisionConfirmed", -600)],
            [_dp(True, -590)],
            None,
            ReservationStatus.FAILED,
            True,
            id="activating-came-up-then-dropped-unsolicited",
        ),
        pytest.param(
            ReservationStatus.ACTIVATING, [], [], None, ReservationStatus.ACTIVATING, False, id="activating-no-anchor"
        ),
        pytest.param(
            ReservationStatus.ACTIVATING,
            [
                OperationResult(
                    result_id=1, timestamp="not-a-timestamp", operation="provisionConfirmed", connection_id="c"
                )
            ],
            [],
            None,
            ReservationStatus.ACTIVATING,
            False,
            id="activating-unparseable-anchor",
        ),
        pytest.param(
            ReservationStatus.ACTIVATING,
            [_result("provisionConfirmed", -600)],
            [],
            _iso(0),
            ReservationStatus.ACTIVATING,
            False,
            id="activating-starttime-in-future-clamps",
        ),
        pytest.param(
            ReservationStatus.ACTIVATING,
            [
                _result("provisionConfirmed", -600, result_id=1),
                _result("releaseConfirmed", -400, result_id=2),
                _result("provisionConfirmed", -10, result_id=3),
            ],
            [_dp(True, -590)],
            None,
            ReservationStatus.ACTIVATING,
            False,
            id="activating-reprovision-uses-latest-epoch",
        ),
        pytest.param(
            ReservationStatus.DEACTIVATING,
            [_result("releaseConfirmed", -10)],
            [],
            None,
            ReservationStatus.DEACTIVATING,
            False,
            id="deactivating-within-grace",
        ),
        pytest.param(
            ReservationStatus.DEACTIVATING,
            [_result("releaseConfirmed", -600)],
            [],
            None,
            ReservationStatus.FAILED,
            True,
            id="deactivating-past-timeout",
        ),
        pytest.param(
            ReservationStatus.DEACTIVATING,
            [],
            [],
            None,
            ReservationStatus.DEACTIVATING,
            False,
            id="deactivating-no-anchor",
        ),
    ],
)
def test_derive_status(
    base: ReservationStatus,
    results: list[OperationResult],
    data_plane_changes: list[DataPlaneStateChange],
    start_time: str | None,
    expected_status: ReservationStatus,
    expect_reason: bool,
) -> None:
    derived = derive_status(
        base,
        results=results,
        data_plane_changes=data_plane_changes,
        start_time=start_time,
        now=_NOW,
        dataplane_timeout=_TIMEOUT,
    )
    assert derived.status == expected_status
    assert (derived.reason is not None) == expect_reason
