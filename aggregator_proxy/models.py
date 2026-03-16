"""Pydantic models for request and response payloads."""

import re
from enum import StrEnum

from pydantic import AnyHttpUrl, BaseModel, Field, field_validator

_UUID_URN_RE = re.compile(
    r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Network URN (NURN) for Service Termination Points.
# Format: urn:ogf:network:<FQDN>:<DATE>:<OPAQUE>[?vlan=<RANGE>][#<FRAGMENT>]
# DATE is YEAR with optional MONTH and DAY (e.g. 2013, 201307, 20130701).
# RANGE is one or more VLAN numbers or ranges (e.g. 1779, 1020-1039, 100,200-300).
_VLAN_RANGE = r"[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*"
_STP_RE = re.compile(
    r"^urn:ogf:network:"
    r"[A-Za-z0-9.\-]+:"           # FQDN
    r"[0-9]{4}(?:[0-9]{2}(?:[0-9]{2})?)?"  # DATE: YYYY[MM[DD]]
    r":[A-Za-z0-9_.:\-]+"         # OPAQUE-PART
    r"(?:\?vlan=" + _VLAN_RANGE + r")?"  # optional ?vlan=RANGE
    r"(?:#[A-Za-z0-9_.:\-]+)?$",  # optional #FRAGMENT
)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ReservationStatus(StrEnum):
    """Simplified connection state-machine states."""

    RESERVING = "RESERVING"
    RESERVED = "RESERVED"
    ACTIVATING = "ACTIVATING"
    ACTIVATED = "ACTIVATED"
    DEACTIVATING = "DEACTIVATING"
    FAILED = "FAILED"
    TERMINATED = "TERMINATED"


# ---------------------------------------------------------------------------
# Shared sub-models
# ---------------------------------------------------------------------------


class P2PS(BaseModel):
    """Point-to-point service parameters."""

    capacity: int = Field(..., gt=0, description="Requested capacity in Mbit/s.")
    sourceSTP: str = Field(..., description="Source Service Termination Point.")
    destSTP: str = Field(..., description="Destination Service Termination Point.")

    @field_validator("sourceSTP", "destSTP")
    @classmethod
    def validate_stp(cls, v: str) -> str:
        """Validate STP is a well-formed Network URN (NURN)."""
        if not _STP_RE.match(v):
            raise ValueError(
                "STP must be a Network URN of the form "
                "urn:ogf:network:<FQDN>:<DATE>:<OPAQUE-PART>[?vlan=<RANGE>]"
            )
        return v


class Criteria(BaseModel):
    """Reservation criteria."""

    serviceType: str | None = Field(
        default=None,
        description="NSI service type URN.",
    )
    p2ps: P2PS


class CriteriaResponse(BaseModel):
    """Reservation criteria as returned in responses (includes version)."""

    version: int
    serviceType: str | None = None
    p2ps: P2PS


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ReservationRequest(BaseModel):
    """Payload for POST /reservations."""

    globalReservationId: str | None = Field(
        default=None,
        description=(
            "Optional globally unique reservation identifier as a UUID URN "
            "(ITU-T X.667 / RFC 4122), e.g. urn:uuid:550e8400-e29b-41d4-a716-446655440000."
        ),
    )
    description: str
    criteria: Criteria
    requesterNSA: str = Field(..., description="NSA URN of the requesting party.")

    @field_validator("globalReservationId")
    @classmethod
    def validate_global_reservation_id(cls, v: str | None) -> str | None:
        """Validate UUID URN format when globalReservationId is supplied."""
        if v is not None and not _UUID_URN_RE.match(v):
            raise ValueError(
                "globalReservationId must be a UUID URN of the form "
                "urn:uuid:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
            )
        return v
    providerNSA: str = Field(..., description="NSA URN of the target aggregator.")
    callbackURL: AnyHttpUrl = Field(
        ..., description="URL to receive the reservation result callback."
    )


class CallbackRequest(BaseModel):
    """Payload for provision, release and terminate requests."""

    callbackURL: AnyHttpUrl = Field(
        ..., description="URL to receive the operation result callback."
    )


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ReservationDetail(BaseModel):
    """Full reservation detail as returned by GET /reservations/{connectionId}."""

    globalReservationId: str | None = None
    connectionId: str
    description: str
    criteria: CriteriaResponse
    status: ReservationStatus


class ReservationsListResponse(BaseModel):
    """Response body for GET /reservations."""

    reservations: list[ReservationDetail]


class AcceptedResponse(BaseModel):
    """202 Accepted response body."""

    type: str
    title: str = "Accepted"
    status: int = 202
    detail: str = "The request is accepted."
    instance: str


# ---------------------------------------------------------------------------
# Error models (returned on 400, 415, 422)
# ---------------------------------------------------------------------------


class FieldError(BaseModel):
    """Single field-level validation error."""

    field: str
    reason: str


class ErrorResponse(BaseModel):
    """Generic error response body."""

    type: str
    title: str
    status: int
    detail: str
    path: str | None = None
    instance: str | None = None
    errors: list[FieldError] | None = None
