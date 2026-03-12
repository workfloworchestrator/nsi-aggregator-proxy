"""Pydantic models for request and response payloads."""

import re
from enum import StrEnum

from pydantic import AnyHttpUrl, BaseModel, Field, field_validator

_UUID_URN_RE = re.compile(
    r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
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
