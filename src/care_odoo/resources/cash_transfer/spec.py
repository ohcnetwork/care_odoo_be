from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class TransferStatus(str, Enum):
    """Status options for cash transfers."""

    pending = "pending"
    accepted = "accepted"
    rejected = "rejected"
    cancelled = "cancelled"


class CreateTransferRequest(BaseModel):
    """Request to create a new cash transfer."""

    from_counter_x_care_id: str = Field(..., description="Sender counter Care ID")
    to_session_id: str = Field(..., description="Receiver session ID")
    amount: Decimal = Field(..., description="Transfer amount")
    denominations: dict[str, int] | None = Field(
        default=None, description="Required for main cash transfers"
    )


class AcceptTransferRequest(BaseModel):
    """Request to accept a cash transfer."""

    counter_x_care_id: str = Field(..., description="Counter Care ID where user is accepting")
    session_id: str = Field(..., description="User's current session ID to validate against transfer destination")


class RejectTransferRequest(BaseModel):
    """Request to reject a cash transfer."""

    counter_x_care_id: str = Field(..., description="Counter Care ID where user is rejecting")
    session_id: str = Field(..., description="User's current session ID to validate against transfer destination")
    reason: str | None = Field(default=None, description="Optional rejection reason")


class CancelTransferRequest(BaseModel):
    """Request to cancel a cash transfer (by sender)."""

    counter_x_care_id: str = Field(..., description="Counter Care ID where user is cancelling")
    reason: str | None = Field(default=None, description="Optional cancellation reason")


class TransferData(BaseModel):
    """Data structure for a cash transfer."""

    id: int
    status: str
    amount: Decimal
    from_session_id: int
    from_user_name: str
    from_counter_name: str
    to_session_id: int
    to_user_name: str
    to_counter_name: str
    created_by_name: str
    created_at: str
    resolved_by_name: str | None = None
    resolved_at: str | None = None
    reject_reason: str | None = None
    denominations: dict[str, int] | None = None


class TransferResponse(BaseModel):
    """Response for transfer operations."""

    success: bool
    transfer: TransferData | None = None
    message: str | None = None


class TransferListResponse(BaseModel):
    """Response for listing transfers."""

    success: bool
    transfers: list[TransferData] = Field(default_factory=list)
    message: str | None = None
