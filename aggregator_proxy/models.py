"""Pydantic models for request and response payloads."""

from enum import StrEnum

from pydantic import AnyHttpUrl, BaseModel, Field


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
        description="Optional globally unique reservation identifier.",
    )
    description: str
    criteria: Criteria
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
