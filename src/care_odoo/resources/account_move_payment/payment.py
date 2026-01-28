import logging
from decimal import Decimal

from care.emr.models.payment_reconciliation import PaymentReconciliation
from care.emr.resources.base import model_from_cache
from care.emr.resources.payment_reconciliation.spec import (
    PaymentReconciliationPaymentMethodOptions,
)
from care.emr.resources.tag.config_spec import TagConfigReadSpec
from rest_framework.exceptions import ValidationError

from care_odoo.connector.connector import OdooConnector
from care_odoo.resources.account_move_payment.spec import (
    AccountMovePaymentApiRequest,
    AccountPaymentCancelApiRequest,
    BillCounterData,
    CustomerType,
    JournalType,
    PaymentMode,
)
from care_odoo.resources.res_partner.spec import PartnerData, PartnerType
from care_odoo.settings import plugin_settings

logger = logging.getLogger(__name__)

# Mapping from PaymentReconciliationPaymentMethodOptions to JournalType
PAYMENT_METHOD_TO_JOURNAL_TYPE: dict[str, JournalType] = {
    PaymentReconciliationPaymentMethodOptions.cash.value: JournalType.cash,  # Cash payment
    PaymentReconciliationPaymentMethodOptions.ccca.value: JournalType.card,  # Credit card
    PaymentReconciliationPaymentMethodOptions.cchk.value: JournalType.bank,  # Certified check
    PaymentReconciliationPaymentMethodOptions.cdac.value: JournalType.bank,  # Checking/debit account
    PaymentReconciliationPaymentMethodOptions.chck.value: JournalType.bank,  # Check
    PaymentReconciliationPaymentMethodOptions.ddpo.value: JournalType.bank,  # Direct deposit/payment order
    PaymentReconciliationPaymentMethodOptions.debc.value: JournalType.debit,  # Debit card
}


class CreditPaymentData:
    """Data class for credit (Care of Account) payments."""

    def __init__(
        self,
        x_care_id: str,
        amount: Decimal,
        payment_method_line_id: int,
        patient_name: str,
        patient_external_id: str,
        patient_phone: str | None,
        payment_date: str,
        counter_external_id: str,
        cashier_external_id: str,
        counter_name: str,
        invoice_external_id: str | None = None,
        reference_number: str | None = None,
    ):
        self.x_care_id = x_care_id
        self.amount = amount
        self.payment_method_line_id = payment_method_line_id
        self.patient_name = patient_name
        self.patient_external_id = patient_external_id
        self.patient_phone = patient_phone
        self.payment_date = payment_date
        self.counter_external_id = counter_external_id
        self.cashier_external_id = cashier_external_id
        self.counter_name = counter_name
        self.invoice_external_id = invoice_external_id
        self.reference_number = reference_number


class OdooPaymentResource:
    # Extension key for credit payment data
    CREDIT_EXTENSION_KEY = "payment_reconciliation_credit_extension"

    def _get_credit_extension_data(self, payment: PaymentReconciliation) -> dict | None:
        """
        Extract credit payment extension data from payment.

        Returns:
            Dict with payment_method_line_id if this is a credit payment, None otherwise

        Raises:
            ValueError: If credit payment is attempted on a refund (credit note)
        """
        if not payment.extensions:
            return None

        credit_ext = payment.extensions.get(self.CREDIT_EXTENSION_KEY)
        if not credit_ext:
            return None

        # Check if is_credit flag is set to True
        is_credit = credit_ext.get("is_credit", False)
        if not is_credit:
            return None

        # Validate: Credit payments cannot be used for refunds
        if payment.is_credit_note:
            raise ValueError(
                "Credit payments (Care of Account) cannot be used for refunds. "
                "Refunds must use the original payment method (cash, card, bank)."
            )

        payment_method_line_id = credit_ext.get("payment_method_line_id")
        if not payment_method_line_id:
            return None

        # Convert string ID to int (matching insurance_company pattern)
        try:
            payment_method_line_id = int(payment_method_line_id)
        except (ValueError, TypeError):
            logger.warning(f"Invalid payment_method_line_id in credit extension: {payment_method_line_id}")
            return None

        return {
            "payment_method_line_id": payment_method_line_id,
        }

    def has_insurance_tag(self, account_tags: list[int], insurance_tag_external_id: str) -> bool:
        """
        Check if any tag in account_tags has an external_id matching insurance_tag_external_id.

        Args:
            account_tags: List of tag database IDs from the account
            insurance_tag_external_id: The external_id (UUID) of the insurance tag from settings

        Returns:
            True if account has the insurance tag, False otherwise
        """
        if not insurance_tag_external_id or not account_tags:
            return False

        for tag_id in account_tags:
            cached_tag = model_from_cache(TagConfigReadSpec, id=tag_id)
            if cached_tag and str(cached_tag.get("id")) == insurance_tag_external_id:
                return True
        return False

    def sync_payment_to_odoo_api(self, payment_id: str) -> int | None:
        """
        Synchronize a Django payment reconciliation to Odoo using the custom addon API.

        Supports both regular payments (cash, card, bank) and credit payments
        (Care of Account - charity/sponsor/fund).

        For credit payments, the payment_method_line_id must be set in the
        payment's extensions field under 'payment_reconciliation_credit_extension'.

        IMPORTANT: Cash payments REQUIRE an open session.

        Args:
            payment_id: External ID of the Django payment reconciliation

        Returns:
            Odoo payment ID if successful, None otherwise

        Raises:
            ValidationError: If cash payment attempted without open session
            ValidationError: If issuer is set but insurance configuration is missing
        """
        payment = PaymentReconciliation.objects.select_related(
            "facility", "account", "account__patient", "target_invoice", "location", "created_by"
        ).get(external_id=payment_id)

        # Check if this is a credit payment (Care of Account)
        credit_data = self._get_credit_extension_data(payment)

        # Handle insurance company id when issuer is set
        if payment.issuer_type == "insurer":
            # Validate insurance configuration
            insurance_tag_id = plugin_settings.CARE_INSURANCE_TAG_ID

            if not insurance_tag_id:
                raise ValidationError("CARE_INSURANCE_TAG_ID must be configured when issuer is set on payment")

            # Check if account has the insurance tag
            # Note: insurance_tag_id is an external_id (UUID), account_tags contains database IDs
            account_tags = payment.account.tags or []
            has_insurance_tag_flag = self.has_insurance_tag(account_tags, insurance_tag_id)

            if not has_insurance_tag_flag:
                raise ValidationError("Account must have insurance tag for insurance payments")

            # Skip without creating payment in Odoo
            return None

        # Prepare partner data
        partner_data = PartnerData(
            name=payment.account.patient.name,
            x_care_id=str(payment.account.patient.external_id),
            partner_type=PartnerType.person,
            phone=payment.account.patient.phone_number,
            state="kerala",
            email="",
            agent=False,
        )

        # Determine journal type and payment_method_line_id
        if credit_data:
            journal_type = JournalType.credit
            payment_method_line_id = credit_data["payment_method_line_id"]
        else:
            journal_type = PAYMENT_METHOD_TO_JOURNAL_TYPE.get(payment.method, JournalType.bank)
            payment_method_line_id = None

        # Prepare payment data
        data = AccountMovePaymentApiRequest(
            journal_x_care_id=str(payment.target_invoice.external_id if payment.target_invoice else ""),
            x_care_id=str(payment.external_id),
            amount=float(payment.amount),
            journal_input=journal_type,
            payment_method_line_id=payment_method_line_id,
            bank_reference=payment.reference_number,
            payment_date=payment.payment_datetime.strftime("%Y-%m-%d"),
            payment_mode=PaymentMode.send if payment.is_credit_note else PaymentMode.receive,
            partner_data=partner_data,
            customer_type=CustomerType.customer,
            counter_data=BillCounterData(
                x_care_id=str(payment.location.external_id),
                cashier_id=str(payment.created_by.external_id),
                counter_name=payment.location.name,
            ),
        ).model_dump()

        logger.info("Odoo Payment Data: %s", data)

        response = OdooConnector.call_api("api/account/move/payment", data)
        return response["payment"]["id"]

    def sync_payment_cancel_to_odoo_api(self, payment_id: str) -> int | None:
        """
        Synchronize a cancelled Django payment reconciliation to Odoo using the custom addon API.

        Args:
            payment_id: External ID of the Django payment reconciliation

        Returns:
            Odoo payment ID if successful, None otherwise
        """
        payment = PaymentReconciliation.objects.get(external_id=payment_id)

        data = AccountPaymentCancelApiRequest(
            x_care_id=str(payment.external_id),
            reason=payment.status,
        ).model_dump()

        logger.info("Odoo Payment Cancel Data: %s", data)

        response = OdooConnector.call_api("api/account/move/payment/cancel", data)
        return response["payment"]["id"]

    def sync_credit_payment_to_odoo_api(self, credit_payment: CreditPaymentData) -> int | None:
        """
        Synchronize a credit (Care of Account) payment to Odoo.

        Credit payments are made by third parties (charities, sponsors, funds) on behalf of patients.
        They require a payment_method_line_id which identifies the specific charity/fund in Odoo.

        Args:
            credit_payment: CreditPaymentData containing payment details

        Returns:
            Odoo payment ID if successful, None otherwise

        Example usage:
            from care_odoo.resources.account_move_payment.payment import (
                OdooPaymentResource, CreditPaymentData
            )

            payment_data = CreditPaymentData(
                x_care_id="CARE-CREDIT-001",
                amount=Decimal("2500.00"),
                payment_method_line_id=42,  # From GET /api/v1/odoo/payment-method-line/
                patient_name="John Doe",
                patient_external_id="PATIENT-001",
                patient_phone="9876543210",
                payment_date="2026-01-21",
                counter_external_id="COUNTER-001",
                cashier_external_id="USER-001",
                counter_name="OP Counter 1",
                invoice_external_id="INV-001",  # Optional - for reconciliation
            )

            odoo_payment_id = OdooPaymentResource().sync_credit_payment_to_odoo_api(payment_data)
        """
        partner_data = PartnerData(
            name=credit_payment.patient_name,
            x_care_id=credit_payment.patient_external_id,
            partner_type=PartnerType.person,
            phone=credit_payment.patient_phone or "",
            state="kerala",
            email="",
            agent=False,
        )

        data = AccountMovePaymentApiRequest(
            journal_x_care_id=credit_payment.invoice_external_id or "",
            x_care_id=credit_payment.x_care_id,
            amount=credit_payment.amount,
            journal_input=JournalType.credit,
            payment_method_line_id=credit_payment.payment_method_line_id,
            bank_reference=credit_payment.reference_number,
            payment_date=credit_payment.payment_date,
            payment_mode=PaymentMode.receive,
            partner_data=partner_data,
            customer_type=CustomerType.customer,
            counter_data=BillCounterData(
                x_care_id=credit_payment.counter_external_id,
                cashier_id=credit_payment.cashier_external_id,
                counter_name=credit_payment.counter_name,
            ),
        ).model_dump()

        logger.info("Odoo Credit Payment Data: %s", data)

        response = OdooConnector.call_api("api/account/move/payment", data)
        return response["payment"]["id"]
