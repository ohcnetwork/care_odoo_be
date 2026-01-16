from pydantic import BaseModel


class InsuranceCompanyData(BaseModel):
    id: int
    name: str
    code: str | bool
    description: str
    account_id: int | None
    account_name: str | None
    active: bool
    claim_count: int

