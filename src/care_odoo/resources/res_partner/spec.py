from enum import Enum

from pydantic import BaseModel


class PartnerType(str, Enum):
    person = "person"
    company = "company"


class PartnerStatus(str, Enum):
    active = "active"
    retired = "retired"
    draft = "draft"


class PartnerData(BaseModel):
    name: str
    x_care_id: str
    email: str
    phone: str
    state: str
    partner_type: PartnerType
    agent: bool
    pan: str | None = None
    status: PartnerStatus | None = None
    gender: str | None = None
    birthdate: str | None = None
    street: str | None = None
