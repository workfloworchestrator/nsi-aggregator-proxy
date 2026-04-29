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


"""Tests for Pydantic model validators."""

import pytest
from pydantic import ValidationError

from aggregator_proxy.models import (
    P2PS,
    Criteria,
    DetailLevel,
    PathSegment,
    ReservationDetail,
    ReservationRequest,
    ReservationStatus,
)


@pytest.mark.parametrize(
    "source_stp",
    [
        pytest.param("urn:ogf:network:example.net:2025:port-1?vlan=100", id="with-vlan"),
        pytest.param("urn:ogf:network:example.net:2025:port-1", id="without-vlan"),
        pytest.param("urn:ogf:network:example.net:2025:port-1?vlan=100-200", id="vlan-range"),
        pytest.param("urn:ogf:network:example.net:2025:port-1?vlan=100,200-300", id="multi-vlan-range"),
        pytest.param("urn:ogf:network:example.net:2025:port-1#in", id="fragment"),
        pytest.param("urn:ogf:network:example.net:2025:port-1?vlan=100#in", id="vlan-and-fragment"),
    ],
)
def test_valid_stp(source_stp: str) -> None:
    p2ps = P2PS(
        capacity=1000,
        sourceSTP=source_stp,
        destSTP="urn:ogf:network:example.net:2025:port-2",
    )
    assert p2ps.sourceSTP == source_stp


def test_valid_stp_date_formats() -> None:
    # YYYY only
    P2PS(capacity=1, sourceSTP="urn:ogf:network:example.net:2025:p", destSTP="urn:ogf:network:example.net:2025:q")
    # YYYYMM
    P2PS(capacity=1, sourceSTP="urn:ogf:network:example.net:202507:p", destSTP="urn:ogf:network:example.net:202507:q")
    # YYYYMMDD
    P2PS(
        capacity=1,
        sourceSTP="urn:ogf:network:example.net:20250701:p",
        destSTP="urn:ogf:network:example.net:20250701:q",
    )


@pytest.mark.parametrize(
    ("source_stp", "dest_stp"),
    [
        pytest.param("http://not-a-urn", "urn:ogf:network:example.net:2025:port-2", id="not-urn"),
        pytest.param(
            "urn:other:network:example.net:2025:port-1", "urn:ogf:network:example.net:2025:port-2", id="wrong-prefix"
        ),
        pytest.param("urn:ogf:network:example.net:2025:port-1", "not-valid", id="invalid-dest"),
    ],
)
def test_invalid_stp(source_stp: str, dest_stp: str) -> None:
    with pytest.raises(ValidationError, match="STP must be a Network URN"):
        P2PS(capacity=1000, sourceSTP=source_stp, destSTP=dest_stp)


def test_invalid_stp_empty() -> None:
    with pytest.raises(ValidationError):
        P2PS(capacity=1000, sourceSTP="", destSTP="urn:ogf:network:example.net:2025:port-2")


@pytest.mark.parametrize(
    "capacity",
    [
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
    ],
)
def test_invalid_capacity_rejected(capacity: int) -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        P2PS(
            capacity=capacity,
            sourceSTP="urn:ogf:network:example.net:2025:p",
            destSTP="urn:ogf:network:example.net:2025:q",
        )


def test_positive_capacity_accepted() -> None:
    p2ps = P2PS(
        capacity=1, sourceSTP="urn:ogf:network:example.net:2025:p", destSTP="urn:ogf:network:example.net:2025:q"
    )
    assert p2ps.capacity == 1


def test_valid_uuid_urn() -> None:
    req = ReservationRequest(
        globalReservationId="urn:uuid:550e8400-e29b-41d4-a716-446655440000",
        description="test",
        criteria=Criteria(
            p2ps=P2PS(
                capacity=1,
                sourceSTP="urn:ogf:network:example.net:2025:p",
                destSTP="urn:ogf:network:example.net:2025:q",
            )
        ),
        requesterNSA="urn:ogf:network:req:2025:nsa",
        providerNSA="urn:ogf:network:prov:2025:nsa",
        callbackURL="http://callback.example.com",  # type: ignore[arg-type]
    )
    assert req.globalReservationId == "urn:uuid:550e8400-e29b-41d4-a716-446655440000"


def test_none_global_reservation_id_accepted() -> None:
    req = ReservationRequest(
        description="test",
        criteria=Criteria(
            p2ps=P2PS(
                capacity=1,
                sourceSTP="urn:ogf:network:example.net:2025:p",
                destSTP="urn:ogf:network:example.net:2025:q",
            )
        ),
        requesterNSA="urn:ogf:network:req:2025:nsa",
        providerNSA="urn:ogf:network:prov:2025:nsa",
        callbackURL="http://callback.example.com",  # type: ignore[arg-type]
    )
    assert req.globalReservationId is None


@pytest.mark.parametrize(
    "global_reservation_id",
    [
        pytest.param("not-a-uuid-urn", id="not-uuid-urn"),
        pytest.param("550e8400-e29b-41d4-a716-446655440000", id="missing-urn-prefix"),
    ],
)
def test_invalid_uuid_urn_rejected(global_reservation_id: str) -> None:
    with pytest.raises(ValidationError, match="UUID URN"):
        ReservationRequest(
            globalReservationId=global_reservation_id,
            description="test",
            criteria=Criteria(
                p2ps=P2PS(
                    capacity=1,
                    sourceSTP="urn:ogf:network:example.net:2025:p",
                    destSTP="urn:ogf:network:example.net:2025:q",
                )
            ),
            requesterNSA="urn:ogf:network:req:2025:nsa",
            providerNSA="urn:ogf:network:prov:2025:nsa",
            callbackURL="http://callback.example.com",  # type: ignore[arg-type]
        )


def test_uppercase_uuid_accepted() -> None:
    req = ReservationRequest(
        globalReservationId="urn:uuid:550E8400-E29B-41D4-A716-446655440000",
        description="test",
        criteria=Criteria(
            p2ps=P2PS(
                capacity=1,
                sourceSTP="urn:ogf:network:example.net:2025:p",
                destSTP="urn:ogf:network:example.net:2025:q",
            )
        ),
        requesterNSA="urn:ogf:network:req:2025:nsa",
        providerNSA="urn:ogf:network:prov:2025:nsa",
        callbackURL="http://callback.example.com",  # type: ignore[arg-type]
    )
    assert req.globalReservationId is not None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("summary", DetailLevel.SUMMARY, id="summary"),
        pytest.param("full", DetailLevel.FULL, id="full"),
        pytest.param("recursive", DetailLevel.RECURSIVE, id="recursive"),
    ],
)
def test_detail_level_values(value: str, expected: DetailLevel) -> None:
    assert DetailLevel(value) == expected


def test_path_segment_without_status() -> None:
    segment = PathSegment(
        order=0,
        connectionId="child-1",
        providerNSA="urn:ogf:network:example.net:2025:nsa:supa",
        capacity=1000,
        sourceSTP="urn:ogf:network:example.net:2025:port-a?vlan=100",
        destSTP="urn:ogf:network:example.net:2025:port-b?vlan=200",
    )
    data = segment.model_dump()
    assert data["status"] is None
    assert data["order"] == 0
    assert data["connectionId"] == "child-1"


def test_path_segment_with_status() -> None:
    segment = PathSegment(
        order=1,
        connectionId="child-2",
        providerNSA="urn:ogf:network:example.net:2025:nsa:supa",
        status=ReservationStatus.ACTIVATED,
    )
    data = segment.model_dump()
    assert data["status"] == "ACTIVATED"


def test_reservation_detail_with_segments() -> None:
    detail = ReservationDetail(
        connectionId="conn-1",
        description="test",
        status=ReservationStatus.ACTIVATED,
        segments=[
            PathSegment(
                order=0,
                connectionId="child-1",
                providerNSA="urn:ogf:network:example.net:2025:nsa:supa",
                capacity=1000,
                status=ReservationStatus.ACTIVATED,
            ),
        ],
    )
    data = detail.model_dump()
    assert data["segments"] is not None
    assert len(data["segments"]) == 1
    assert data["segments"][0]["connectionId"] == "child-1"


def test_reservation_detail_without_segments() -> None:
    detail = ReservationDetail(
        connectionId="conn-1",
        description="test",
        status=ReservationStatus.RESERVED,
    )
    data = detail.model_dump()
    assert data["segments"] is None
