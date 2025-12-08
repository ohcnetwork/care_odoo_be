from enum import Enum

from pydantic import BaseModel

from care_odoo.resources.res_partner.spec import PartnerData


class UserType(str, Enum):
    portal = "portal"
    internal = "internal"


class UserData(BaseModel):
    x_care_id: str
    name: str
    login: str
    email: str
    user_type: UserType
    phone: str
    state: str
    partner_data: PartnerData
