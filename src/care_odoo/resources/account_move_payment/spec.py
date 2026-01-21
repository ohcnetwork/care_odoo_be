from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, model_validator

from care_odoo.resources.res_partner.spec import PartnerData


class JournalType(str, Enum):
    cash = "cash"
    bank = "bank"
    card = "card"
    credit = "credit"  # Care of Accounts (charity/sponsor payments)


class PaymentMode(str, Enum):
    send = "send"
    receive = "receive"


class CustomerType(str, Enum):
    customer = "customer"
    vendor = "vendor"


class BillCounterData(BaseModel):
    x_care_id: str
    cashier_id: str
    counter_name: str


class AccountMovePaymentApiRequest(BaseModel):
    x_care_id: str
    journal_x_care_id: str | None = None
    amount: Decimal = Decimal("0.0")
    journal_input: JournalType
    payment_date: str
    payment_mode: PaymentMode
    partner_data: PartnerData
    customer_type: CustomerType
    counter_data: BillCounterData
    bank_reference: str | None = None
    # For credit payments - specifies which charity/fund is paying
    payment_method_line_id: int | None = None

    @model_validator(mode="after")
    def validate_payment_method_line(self):
        """Validate that credit payments include payment_method_line_id."""
        if self.journal_input == JournalType.credit and not self.payment_method_line_id:
            raise ValueError(
                "payment_method_line_id is required for credit (Care of Account) payments. "
                "Use GET /api/v1/odoo/payment-method-line/ to fetch available payment methods."
            )
        return self


class AccountPaymentCancelApiRequest(BaseModel):
    x_care_id: str
    reason: str | None = None
