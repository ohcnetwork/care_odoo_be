from pydantic import BaseModel


class PaymentMethodLineData(BaseModel):
    """
    Represents a payment method line from Odoo.

    Payment method lines are used for Credit payments (Care of Accounts):
    charity, sponsor, fund payments on behalf of patients.

    Each payment method line is tied to a specific journal (e.g., Charity Journal).
    """

    id: int
    name: str
    code: str | None = None
    journal_id: int
    journal_name: str
