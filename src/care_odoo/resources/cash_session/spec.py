from enum import Enum

from pydantic import BaseModel, Field


class SessionStatus(str, Enum):
    """Status options for cash sessions."""

    open = "open"
    closed = "closed"


class OpenSessionRequest(BaseModel):
    """Request to open a new cash session."""

    counter_x_care_id: str = Field(..., description="Counter/location Care ID")
    opening_balance: float = Field(default=0.0, description="Opening balance amount")


class CloseSessionRequest(BaseModel):
    """Request to close an existing cash session."""

    counter_x_care_id: str = Field(..., description="Counter/location Care ID")

class SessionData(BaseModel):
    """Data structure for a cash session."""

    id: int
    status: str
    opening_balance: float
    expected_amount: float
    counter_id: int
    counter_x_care_id: str
    external_user_id: str
    external_user_name: str
    counter_name: str
    opened_at: str
    closed_at: str | None = None
    closing_expected: float
    closing_declared: float
    closing_difference: float
    difference_status: str | None = None
    payment_count: int
    pending_outgoing_count: int
    pending_incoming_count: int


class SessionResponse(BaseModel):
    """Response for session operations."""

    success: bool
    session: SessionData | None = None
    message: str | None = None


class SessionListResponse(BaseModel):
    """Response for listing sessions."""

    success: bool
    sessions: list[SessionData] = Field(default_factory=list)
    message: str | None = None


class OpenSessionInfo(BaseModel):
    """Info about an open session at a counter."""
    session_id: int
    external_user_id: str
    external_user_name: str

class CounterData(BaseModel):
    """Data structure for a cash counter."""

    id: int
    name: str
    x_care_id: str
    is_main_cash: bool = False
    has_open_session: bool = False
    open_sessions: list[OpenSessionInfo] = Field(default_factory=list)
    open_session_count: int = 0


class CounterListResponse(BaseModel):
    """Response for listing counters."""

    success: bool
    counters: list[CounterData] = Field(default_factory=list)
    count: int = 0
    message: str | None = None
