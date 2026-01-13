import logging

from care.emr.models.payment_reconciliation import PaymentReconciliation
from care.emr.resources.payment_reconciliation.spec import (
    PaymentReconciliationPaymentMethodOptions,
)
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

logger = logging.getLogger(__name__)

# Mapping from PaymentReconciliationPaymentMethodOptions to JournalType
PAYMENT_METHOD_TO_JOURNAL_TYPE: dict[str, JournalType] = {
    PaymentReconciliationPaymentMethodOptions.cash.value: JournalType.cash,  # Cash payment
    PaymentReconciliationPaymentMethodOptions.ccca.value: JournalType.card,  # Credit card
    PaymentReconciliationPaymentMethodOptions.cchk.value: JournalType.bank,  # Certified check
    PaymentReconciliationPaymentMethodOptions.cdac.value: JournalType.bank,  # Checking/debit account
    PaymentReconciliationPaymentMethodOptions.chck.value: JournalType.bank,  # Check
    PaymentReconciliationPaymentMethodOptions.ddpo.value: JournalType.bank,  # Direct deposit/payment order
    PaymentReconciliationPaymentMethodOptions.debc.value: JournalType.bank,  # Debit card
}


class OdooPaymentResource:
    def sync_payment_to_odoo_api(self, payment_id: str) -> int | None:
        """
        Synchronize a Django payment reconciliation to Odoo using the custom addon API.

        IMPORTANT: Cash payments now REQUIRE an open session.

        Args:
            payment_id: External ID of the Django payment reconciliation

        Returns:
            Odoo payment ID if successful, None otherwise

        Raises:
            ValidationError: If cash payment attempted without open session
        """
        payment = PaymentReconciliation.objects.select_related("facility", "account", "target_invoice").get(
            external_id=payment_id
        )

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

        # Prepare payment data
        data = AccountMovePaymentApiRequest(
            journal_x_care_id=str(payment.target_invoice.external_id if payment.target_invoice else ""),
            x_care_id=str(payment.external_id),
            amount=float(payment.amount),
            journal_input=PAYMENT_METHOD_TO_JOURNAL_TYPE.get(payment.method, JournalType.bank),
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
