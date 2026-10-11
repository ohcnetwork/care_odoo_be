from enum import Enum

from pydantic import BaseModel, field_validator


class PartnerType(str, Enum):
    person = "person"
    company = "company"


class PartnerStatus(str, Enum):
    active = "active"
    retired = "retired"
    draft = "draft"


class GenderType(str, Enum):
    male = "male"
    female = "female"
    other = "other"


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
    gender: GenderType | None = None
    birthdate: str | None = None
    street: str | None = None
    ref: str | None = None

    @field_validator("gender", mode="before")
    @classmethod
    def map_gender(cls, gender: str | None) -> str | None:
        """Odoo only knows male, female and other, so Care's other genders (non_binary, transgender) map to other."""
        if not gender:
            return None
        return gender if gender in (GenderType.male, GenderType.female) else GenderType.other
