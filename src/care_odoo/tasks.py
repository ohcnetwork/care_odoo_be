import logging
import time

from celery import shared_task
from requests.exceptions import ConnectionError, Timeout

from care_odoo.connector.connector import OdooConnector
from care_odoo.resources.account_move_payment.spec import AccountPaymentCancelApiRequest

logger = logging.getLogger(__name__)

# Seconds to wait before re-checking if record truly doesn't exist
# This helps avoid false positives from slow-committing transactions
RECHECK_DELAY_SECONDS = 5


@shared_task(
    bind=True,
    name="care_odoo.tasks.verify_payment_exists_or_cleanup",
    max_retries=3,
    default_retry_delay=10,
    autoretry_for=(ConnectionError, Timeout),
)
def verify_payment_exists_or_cleanup(self, payment_external_id: str) -> dict:
    """
    Verify that a payment reconciliation exists in the Care database.
    If it doesn't exist (transaction rolled back), cancel the payment in Odoo.

    This task is scheduled with a delay after creating a payment in Odoo to handle
    the case where the Odoo API call succeeds but the Care transaction rolls back.

    Uses a double-check mechanism: if the record is not found on first check,
    waits a few seconds and checks again to avoid false positives from
    slow-committing transactions.

    Args:
        payment_external_id: The external_id of the PaymentReconciliation

    Returns:
        dict with status and action taken
    """
    # Import here to avoid circular imports
    from care.emr.models.payment_reconciliation import PaymentReconciliation

    logger.info(
        "Verifying payment exists in Care DB: %s",
        payment_external_id,
    )

    # First check
    payment_exists = PaymentReconciliation.objects.filter(
        external_id=payment_external_id
    ).exists()

    if payment_exists:
        logger.info(
            "Payment %s exists in Care DB. No cleanup needed.",
            payment_external_id,
        )
        return {
            "status": "success",
            "action": "none",
            "message": f"Payment {payment_external_id} exists in Care DB",
        }

    # Record not found - but transaction might still be committing
    # Wait and check again to avoid false positives
    logger.info(
        "Payment %s not found on first check. Waiting %ds before re-checking...",
        payment_external_id,
        RECHECK_DELAY_SECONDS,
    )
    time.sleep(RECHECK_DELAY_SECONDS)

    # Second check - if still not found, proceed with cleanup
    payment_exists = PaymentReconciliation.objects.filter(
        external_id=payment_external_id
    ).exists()

    if payment_exists:
        logger.info(
            "Payment %s found on second check. Transaction committed late. No cleanup needed.",
            payment_external_id,
        )
        return {
            "status": "success",
            "action": "none",
            "message": f"Payment {payment_external_id} exists in Care DB (found on recheck)",
        }

    # Payment confirmed not in Care DB - the transaction must have rolled back
    # We need to cancel/delete the payment in Odoo
    logger.warning(
        "Payment %s NOT found in Care DB after double-check. Initiating Odoo cleanup.",
        payment_external_id,
    )

    try:
        data = AccountPaymentCancelApiRequest(
            x_care_id=str(payment_external_id),
            reason="Care transaction rollback cleanup",
        ).model_dump()

        logger.info("Odoo Payment Cleanup Data: %s", data)

        response = OdooConnector.call_api("api/account/move/payment/cancel", data)

        logger.info(
            "Successfully cleaned up payment %s from Odoo. Response: %s",
            payment_external_id,
            response,
        )

        return {
            "status": "success",
            "action": "cleanup",
            "message": f"Payment {payment_external_id} cleaned up from Odoo",
            "odoo_response": response,
        }
    except Exception as e:
        logger.exception(
            "Failed to cleanup payment %s from Odoo: %s",
            payment_external_id,
            str(e),
        )
        # Re-raise to trigger Celery retry
        raise


@shared_task(
    bind=True,
    name="care_odoo.tasks.verify_invoice_exists_or_cleanup",
    max_retries=3,
    default_retry_delay=10,
    autoretry_for=(ConnectionError, Timeout),
)
def verify_invoice_exists_or_cleanup(self, invoice_external_id: str) -> dict:
    """
    Verify that an invoice exists in the Care database.
    If it doesn't exist (transaction rolled back), cancel the invoice in Odoo.

    This task is scheduled with a delay after creating an invoice in Odoo to handle
    the case where the Odoo API call succeeds but the Care transaction rolls back.

    Uses a double-check mechanism: if the record is not found on first check,
    waits a few seconds and checks again to avoid false positives from
    slow-committing transactions.

    Args:
        invoice_external_id: The external_id of the Invoice

    Returns:
        dict with status and action taken
    """
    # Import here to avoid circular imports
    from care.emr.models.invoice import Invoice

    from care_odoo.resources.account_move.spec import AccountMoveReturnApiRequest

    logger.info(
        "Verifying invoice exists in Care DB: %s",
        invoice_external_id,
    )

    # First check
    invoice_exists = Invoice.objects.filter(
        external_id=invoice_external_id
    ).exists()

    if invoice_exists:
        logger.info(
            "Invoice %s exists in Care DB. No cleanup needed.",
            invoice_external_id,
        )
        return {
            "status": "success",
            "action": "none",
            "message": f"Invoice {invoice_external_id} exists in Care DB",
        }

    # Record not found - but transaction might still be committing
    # Wait and check again to avoid false positives
    logger.info(
        "Invoice %s not found on first check. Waiting %ds before re-checking...",
        invoice_external_id,
        RECHECK_DELAY_SECONDS,
    )
    time.sleep(RECHECK_DELAY_SECONDS)

    # Second check - if still not found, proceed with cleanup
    invoice_exists = Invoice.objects.filter(
        external_id=invoice_external_id
    ).exists()

    if invoice_exists:
        logger.info(
            "Invoice %s found on second check. Transaction committed late. No cleanup needed.",
            invoice_external_id,
        )
        return {
            "status": "success",
            "action": "none",
            "message": f"Invoice {invoice_external_id} exists in Care DB (found on recheck)",
        }

    # Invoice confirmed not in Care DB - the transaction must have rolled back
    # We need to return/cancel the invoice in Odoo
    logger.warning(
        "Invoice %s NOT found in Care DB after double-check. Initiating Odoo cleanup.",
        invoice_external_id,
    )

    try:
        data = AccountMoveReturnApiRequest(
            x_care_id=str(invoice_external_id),
            reason="Care transaction rollback cleanup",
        ).model_dump()

        logger.info("Odoo Invoice Cleanup Data: %s", data)

        response = OdooConnector.call_api("api/account/move/return", data)

        logger.info(
            "Successfully cleaned up invoice %s from Odoo. Response: %s",
            invoice_external_id,
            response,
        )

        return {
            "status": "success",
            "action": "cleanup",
            "message": f"Invoice {invoice_external_id} cleaned up from Odoo",
            "odoo_response": response,
        }
    except Exception as e:
        logger.exception(
            "Failed to cleanup invoice %s from Odoo: %s",
            invoice_external_id,
            str(e),
        )
        # Re-raise to trigger Celery retry
        raise
