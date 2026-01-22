import logging

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
    PaymentReconciliationPaymentMethodOptions.debc.value: JournalType.bank,  # Debit card
}


class OdooPaymentResource:
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

        IMPORTANT: Cash payments now REQUIRE an open session.

        Args:
            payment_id: External ID of the Django payment reconciliation

        Returns:
            Odoo payment ID if successful, None otherwise

        Raises:
            ValidationError: If cash payment attempted without open session
            ValidationError: If issuer is set but insurance configuration is missing
        """
        payment = PaymentReconciliation.objects.select_related("facility", "account", "target_invoice").get(
            external_id=payment_id
        )

        # Handle insurance company id when issuer is set
        if payment.issuer_type == "insurer":
            # Validate insurance configuration
            insurance_tag_id = plugin_settings.CARE_INSURANCE_TAG_ID
            insurance_extension_name = plugin_settings.CARE_ODOO_INSURANCE_EXTENSION_NAME

            if not insurance_extension_name:
                raise ValidationError(
                    "CARE_ODOO_INSURANCE_EXTENSION_NAME must be configured when issuer is set on payment"
                )

            if not insurance_tag_id:
                raise ValidationError("CARE_INSURANCE_TAG_ID must be configured when issuer is set on payment")

            # Check if account has the insurance tag
            # Note: insurance_tag_id is an external_id (UUID), account_tags contains database IDs
            account_tags = payment.account.tags or []
            has_insurance_tag_flag = self.has_insurance_tag(account_tags, insurance_tag_id)

            if not has_insurance_tag_flag:
                raise ValidationError("Account must have insurance tag for insurance payments")

            # Get insurance company id from account extensions
            insurance_company_id_raw = (
                payment.account.extensions.get("account_extension", {}).get(insurance_extension_name)
                if payment.account.extensions.get("account_extension")
                and insurance_extension_name in payment.account.extensions.get("account_extension", {})
                else None
            )

            if not insurance_company_id_raw:
                raise ValidationError("Account must have insurance company id when issuer is set on insurance")

            # Convert to int for the API request
            try:
                int(insurance_company_id_raw)
            except (ValueError, TypeError) as e:
                raise ValidationError(
                    f"Invalid insurance company id '{insurance_company_id_raw}' - must be a valid integer"
                ) from e

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
