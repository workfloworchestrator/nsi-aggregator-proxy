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

from aggregator_proxy.models import P2PS, Criteria, ReservationRequest


class TestSTPValidator:
    """Test the STP (Service Termination Point) regex validator."""

    def test_valid_stp_with_vlan(self) -> None:
        p2ps = P2PS(
            capacity=1000,
            sourceSTP="urn:ogf:network:example.net:2025:port-1?vlan=100",
            destSTP="urn:ogf:network:example.net:2025:port-2?vlan=200",
        )
        assert p2ps.sourceSTP == "urn:ogf:network:example.net:2025:port-1?vlan=100"

    def test_valid_stp_without_vlan(self) -> None:
        p2ps = P2PS(
            capacity=1000,
            sourceSTP="urn:ogf:network:example.net:2025:port-1",
            destSTP="urn:ogf:network:example.net:2025:port-2",
        )
        assert p2ps.sourceSTP == "urn:ogf:network:example.net:2025:port-1"

    def test_valid_stp_with_vlan_range(self) -> None:
        p2ps = P2PS(
            capacity=1000,
            sourceSTP="urn:ogf:network:example.net:2025:port-1?vlan=100-200",
            destSTP="urn:ogf:network:example.net:2025:port-2?vlan=300",
        )
        assert p2ps.sourceSTP == "urn:ogf:network:example.net:2025:port-1?vlan=100-200"

    def test_valid_stp_with_multi_vlan_range(self) -> None:
        p2ps = P2PS(
            capacity=1000,
            sourceSTP="urn:ogf:network:example.net:2025:port-1?vlan=100,200-300",
            destSTP="urn:ogf:network:example.net:2025:port-2?vlan=400",
        )
        assert p2ps.sourceSTP == "urn:ogf:network:example.net:2025:port-1?vlan=100,200-300"

    def test_valid_stp_with_fragment(self) -> None:
        p2ps = P2PS(
            capacity=1000,
            sourceSTP="urn:ogf:network:example.net:2025:port-1#in",
            destSTP="urn:ogf:network:example.net:2025:port-2#out",
        )
        assert p2ps.sourceSTP == "urn:ogf:network:example.net:2025:port-1#in"

    def test_valid_stp_with_vlan_and_fragment(self) -> None:
        p2ps = P2PS(
            capacity=1000,
            sourceSTP="urn:ogf:network:example.net:2025:port-1?vlan=100#in",
            destSTP="urn:ogf:network:example.net:2025:port-2?vlan=200#out",
        )
        assert p2ps.sourceSTP == "urn:ogf:network:example.net:2025:port-1?vlan=100#in"

    def test_valid_stp_date_formats(self) -> None:
        # YYYY only
        P2PS(capacity=1, sourceSTP="urn:ogf:network:example.net:2025:p", destSTP="urn:ogf:network:example.net:2025:q")
        # YYYYMM
        P2PS(
            capacity=1, sourceSTP="urn:ogf:network:example.net:202507:p", destSTP="urn:ogf:network:example.net:202507:q"
        )
        # YYYYMMDD
        P2PS(
            capacity=1,
            sourceSTP="urn:ogf:network:example.net:20250701:p",
            destSTP="urn:ogf:network:example.net:20250701:q",
        )

    def test_invalid_stp_not_urn(self) -> None:
        with pytest.raises(ValidationError, match="STP must be a Network URN"):
            P2PS(capacity=1000, sourceSTP="http://not-a-urn", destSTP="urn:ogf:network:example.net:2025:port-2")

    def test_invalid_stp_wrong_prefix(self) -> None:
        with pytest.raises(ValidationError, match="STP must be a Network URN"):
            P2PS(
                capacity=1000,
                sourceSTP="urn:other:network:example.net:2025:port-1",
                destSTP="urn:ogf:network:example.net:2025:port-2",
            )

    def test_invalid_stp_empty(self) -> None:
        with pytest.raises(ValidationError):
            P2PS(capacity=1000, sourceSTP="", destSTP="urn:ogf:network:example.net:2025:port-2")

    def test_invalid_dest_stp(self) -> None:
        with pytest.raises(ValidationError, match="STP must be a Network URN"):
            P2PS(capacity=1000, sourceSTP="urn:ogf:network:example.net:2025:port-1", destSTP="not-valid")


class TestCapacityValidator:
    """Test capacity field constraints."""

    def test_zero_capacity_rejected(self) -> None:
        with pytest.raises(ValidationError, match="greater than 0"):
            P2PS(
                capacity=0, sourceSTP="urn:ogf:network:example.net:2025:p", destSTP="urn:ogf:network:example.net:2025:q"
            )

    def test_negative_capacity_rejected(self) -> None:
        with pytest.raises(ValidationError, match="greater than 0"):
            P2PS(
                capacity=-1,
                sourceSTP="urn:ogf:network:example.net:2025:p",
                destSTP="urn:ogf:network:example.net:2025:q",
            )

    def test_positive_capacity_accepted(self) -> None:
        p2ps = P2PS(
            capacity=1, sourceSTP="urn:ogf:network:example.net:2025:p", destSTP="urn:ogf:network:example.net:2025:q"
        )
        assert p2ps.capacity == 1


class TestGlobalReservationIdValidator:
    """Test UUID URN validation on globalReservationId."""

    def test_valid_uuid_urn(self) -> None:
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

    def test_none_global_reservation_id_accepted(self) -> None:
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

    def test_invalid_uuid_urn_rejected(self) -> None:
        with pytest.raises(ValidationError, match="UUID URN"):
            ReservationRequest(
                globalReservationId="not-a-uuid-urn",
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

    def test_uuid_without_urn_prefix_rejected(self) -> None:
        with pytest.raises(ValidationError, match="UUID URN"):
            ReservationRequest(
                globalReservationId="550e8400-e29b-41d4-a716-446655440000",
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

    def test_uppercase_uuid_accepted(self) -> None:
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
