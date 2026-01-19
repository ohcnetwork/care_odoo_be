import logging

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from care.emr.models.charge_item_definition import ChargeItemDefinition
from care.emr.models.invoice import Invoice
from care.emr.models.organization import Organization
from care.emr.models.payment_reconciliation import PaymentReconciliation
from care.emr.models.product import Product
from care.emr.models.resource_category import ResourceCategory
from care.emr.models.supply_delivery import DeliveryOrder
from care.emr.resources.inventory.supply_delivery.delivery_order import (
    SupplyDeliveryOrderStatusOptions,
)
from care.emr.resources.invoice.spec import (
    INVOICE_CANCELLED_STATUS,
    InvoiceStatusOptions,
)
from care.emr.resources.organization.spec import OrganizationTypeChoices
from care.emr.resources.payment_reconciliation.spec import (
    PaymentReconciliationStatusOptions,
)
from care.emr.resources.resource_category.spec import (
    ResourceCategoryResourceTypeOptions,
)
from care.users.models import User
from care_odoo.resources.account_move.delivery_order import OdooDeliveryOrderResource
from care_odoo.resources.account_move.invoice import OdooInvoiceResource
from care_odoo.resources.account_move_payment.payment import OdooPaymentResource
from care_odoo.resources.product_category.category import OdooCategoryResource
from care_odoo.resources.product_product.resource import OdooProductProductResource
from care_odoo.resources.res_partner.resource import OdooPartnerResource
from care_odoo.resources.res_user.resource import OdooUserResource
from care_odoo.settings import plugin_settings
from care_odoo.tasks import verify_invoice_exists_or_cleanup, verify_payment_exists_or_cleanup

logger = logging.getLogger(__name__)


@receiver(post_save, sender=User)
def sync_user_to_odoo(sender, instance, created, **kwargs):
    """
    Signal handler to sync user to Odoo when created or updated.
    """
    odoo_user = OdooUserResource()
    odoo_user.sync_user_to_odoo_api(instance)


@receiver(pre_save, sender=Invoice)
def capture_previous_status(sender, instance, **kwargs):
    """
    Capture the previous status value before save to access it in post_save signal.
    """
    if instance.pk:
        old_instance = Invoice.objects.get(pk=instance.pk)
        instance._previous_status = old_instance.status
    else:
        instance._previous_status = None


@receiver(post_save, sender=Invoice)
def save_fields_before_update(sender, instance, raw, using, update_fields, **kwargs):
    """
    Signal handler to sync invoice to Odoo when status changes.

    After successfully creating an invoice in Odoo, schedules a cleanup task
    to verify the invoice exists in Care DB after the transaction completes.
    If the Care transaction rolls back, the cleanup task will cancel the
    orphaned invoice in Odoo.
    """
    # Skip sync if only 'number' field is being updated
    if update_fields and update_fields == {"number"}:
        return

    # Access previous status if needed: getattr(instance, "_previous_status", None)
    # current_status = instance.status

    if instance.status in [
        InvoiceStatusOptions.issued.value,
    ]:
        odoo_integration = OdooInvoiceResource()
        odoo_integration.sync_invoice_to_odoo_api(instance.external_id)

        # Schedule cleanup task to verify invoice exists after transaction completes
        # The task runs after a delay to give the transaction time to commit or rollback
        # If the transaction rolled back, this task will clean up the orphaned Odoo invoice
        cleanup_delay = plugin_settings.CARE_ODOO_CLEANUP_DELAY_SECONDS
        logger.info(
            "Scheduling invoice cleanup verification task for %s in %d seconds",
            instance.external_id,
            cleanup_delay,
        )
        verify_invoice_exists_or_cleanup.apply_async(
            args=[str(instance.external_id)],
            countdown=cleanup_delay,
        )
    elif instance.status in INVOICE_CANCELLED_STATUS and instance._previous_status in [
        InvoiceStatusOptions.issued.value,
        InvoiceStatusOptions.balanced.value,
    ]:
        odoo_integration = OdooInvoiceResource()
        odoo_integration.sync_invoice_return_to_odoo_api(instance.external_id)


@receiver(post_save, sender=PaymentReconciliation)
def sync_payment_to_odoo(sender, instance, created, **kwargs):
    """
    Signal handler to sync payment reconciliation to Odoo when created.

    After successfully creating a payment in Odoo, schedules a cleanup task
    to verify the payment exists in Care DB after the transaction completes.
    If the Care transaction rolls back, the cleanup task will cancel the
    orphaned payment in Odoo.
    """
    if instance.status == PaymentReconciliationStatusOptions.active.value:
        odoo_payment = OdooPaymentResource()
        odoo_payment.sync_payment_to_odoo_api(instance.external_id)

        # Schedule cleanup task to verify payment exists after transaction completes
        # The task runs after a delay to give the transaction time to commit or rollback
        # If the transaction rolled back, this task will clean up the orphaned Odoo payment
        cleanup_delay = plugin_settings.CARE_ODOO_CLEANUP_DELAY_SECONDS
        logger.info(
            "Scheduling payment cleanup verification task for %s in %d seconds",
            instance.external_id,
            cleanup_delay,
        )
        verify_payment_exists_or_cleanup.apply_async(
            args=[str(instance.external_id)],
            countdown=cleanup_delay,
        )
    elif instance.status in [
        PaymentReconciliationStatusOptions.cancelled.value,
        PaymentReconciliationStatusOptions.entered_in_error.value,
    ]:
        odoo_payment = OdooPaymentResource()
        odoo_payment.sync_payment_cancel_to_odoo_api(instance.external_id)


@receiver(post_save, sender=ChargeItemDefinition)
def sync_charge_item_definition_to_odoo(sender, instance, created, **kwargs):
    """
    Signal handler to sync charge item definition to Odoo as a product when created or updated.
    """
    odoo_product = OdooProductProductResource()
    odoo_product.sync_product_to_odoo_api(instance)


@receiver(post_save, sender=ResourceCategory)
def sync_resource_category_to_odoo(sender, instance, created, **kwargs):
    """
    Signal handler to sync resource category to Odoo when created or updated.
    """
    if instance.resource_type == ResourceCategoryResourceTypeOptions.charge_item_definition.value:
        odoo_category = OdooCategoryResource()
        odoo_category.sync_category_to_odoo_api(instance)


@receiver(post_save, sender=Organization)
def sync_organization_to_odoo(sender, instance, created, **kwargs):
    """
    Signal handler to sync organization to Odoo as a partner when org_type is product_supplier.
    """
    if instance.org_type == OrganizationTypeChoices.product_supplier.value:
        odoo_partner = OdooPartnerResource()
        odoo_partner.sync_partner_to_odoo_api(instance)


@receiver(post_save, sender=DeliveryOrder)
def sync_delivery_order_to_odoo(sender, instance, created, **kwargs):
    """
    Signal handler to sync delivery order to Odoo as a vendor bill when completed.
    """
    if instance.status == SupplyDeliveryOrderStatusOptions.completed.value and not instance.origin:
        odoo_delivery_order = OdooDeliveryOrderResource()
        odoo_delivery_order.sync_delivery_order_to_odoo_api(instance.external_id)


@receiver(post_save, sender=Product)
def sync_product_to_odoo(sender, instance, created, **kwargs):
    """
    Signal handler to sync product to Odoo when it has a charge item definition.
    """
    odoo_product = OdooProductProductResource()
    odoo_product.sync_product_from_product_model(instance)
