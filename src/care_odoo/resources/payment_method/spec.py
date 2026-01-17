from pydantic import BaseModel


class SponsorData(BaseModel):
    id: int
    name: str
    code: str | None = ""
    phone: str | None = ""
    email: str | None = ""
    city: str | None = ""
    account_id: int | None = None
    account_name: str | None = ""
    active: bool = True
    invoice_count: int = 0


class SetOdooSponsorRequest(BaseModel):
    odoo_sponsor_id: int | None = None
