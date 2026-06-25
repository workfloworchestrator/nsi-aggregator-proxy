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


"""Map NSI connection sub-state machines to the simplified proxy state."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aggregator_proxy.models import ReservationStatus
from aggregator_proxy.nsi_soap.parser import ConnectionStates, DataPlaneStateChange, OperationResult


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


@dataclass
class DerivedStatus:
    """A status derived from the aggregator, with a synthetic reason when the deriver fails it."""

    status: ReservationStatus
    reason: str | None = None


def parse_iso8601(timestamp: str) -> datetime | None:
    """Parse an ISO8601 timestamp to a tz-aware datetime, or None if it cannot be parsed.

    Accepts a trailing ``Z`` and offsets; a naive timestamp is assumed to be UTC. Aggregator
    timestamps are on the aggregator's clock — a few seconds of skew is negligible against the
    data-plane timeout, so no skew correction is applied.
    """
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _latest_operation_time(results: list[OperationResult], operation: str) -> datetime | None:
    """Return the timestamp of the most recent result for ``operation`` (by resultId, then timestamp)."""
    matching = [result for result in results if result.operation == operation]
    if not matching:
        return None
    latest = max(matching, key=lambda result: (result.result_id, result.timestamp))
    return parse_iso8601(latest.timestamp)


def _data_plane_active_since(data_plane_changes: list[DataPlaneStateChange], since: datetime) -> bool:
    """True if the data plane reported active at or after ``since``."""
    return any(
        change.active and (ts := parse_iso8601(change.timestamp)) is not None and ts >= since
        for change in data_plane_changes
    )


def derive_status(
    base: ReservationStatus,
    *,
    results: list[OperationResult],
    data_plane_changes: list[DataPlaneStateChange],
    start_time: str | None,
    now: datetime,
    dataplane_timeout: timedelta,
) -> DerivedStatus:
    """Refine the transient ``ACTIVATING``/``DEACTIVATING`` base status using durable aggregator data.

    For a provision whose data plane never came up within ``dataplane_timeout`` of the last
    provisionConfirmed (clamped to the reservation startTime), the connection is FAILED. A data plane
    that came up and then went inactive while still provisioned is an unsolicited drop (no release or
    terminate) — which in this deployment never recovers — so it is also FAILED. The release side is
    symmetric. Every other base status passes through unchanged.
    """
    timeout_seconds = int(dataplane_timeout.total_seconds())
    schedule_start = parse_iso8601(start_time) if start_time is not None else None

    match base:
        case ReservationStatus.ACTIVATING:
            provision_at = _latest_operation_time(results, "provisionConfirmed")
            if provision_at is None:
                return DerivedStatus(base)
            if _data_plane_active_since(data_plane_changes, provision_at):
                return DerivedStatus(
                    ReservationStatus.FAILED,
                    "data plane went inactive after activation without a release or terminate",
                )
            deadline = max(provision_at, schedule_start) if schedule_start is not None else provision_at
            if now - deadline > dataplane_timeout:
                return DerivedStatus(
                    ReservationStatus.FAILED,
                    f"data plane not active within {timeout_seconds}s of provision at {provision_at.isoformat()}",
                )
            return DerivedStatus(base)

        case ReservationStatus.DEACTIVATING:
            release_at = _latest_operation_time(results, "releaseConfirmed")
            if release_at is None:
                return DerivedStatus(base)
            if now - release_at > dataplane_timeout:
                return DerivedStatus(
                    ReservationStatus.FAILED,
                    f"data plane still active {timeout_seconds}s after release at {release_at.isoformat()}",
                )
            return DerivedStatus(base)

        case _:
            return DerivedStatus(base)
