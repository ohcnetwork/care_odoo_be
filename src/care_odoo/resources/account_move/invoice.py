import logging

from care.emr.models.charge_item import ChargeItem
from care.emr.models.invoice import Invoice
from care.emr.models.medication_dispense import MedicationDispense
from care.emr.models.scheduling.booking import TokenBooking
from care.emr.models.service_request import ServiceRequest
from care.emr.resources.charge_item.spec import ChargeItemResourceOptions
from care_odoo.connector.connector import OdooConnector
from care_odoo.resources.account_move.spec import (
    AccountMoveApiRequest,
    AccountMoveReturnApiRequest,
    BillType,
    InvoiceItem,
)
from care_odoo.resources.product_category.spec import CategoryData
from care_odoo.resources.product_product.spec import ProductData, TaxData
from care_odoo.resources.res_partner.spec import PartnerData, PartnerType
from care_odoo.resources.utils import (
    get_all_discounts,
    get_base_price_from_charge_item,
    get_purchase_price_from_charge_item,
    get_taxes_from_definition,
)

logger = logging.getLogger(__name__)


class OdooInvoiceResource:
    def sync_invoice_to_odoo_api(self, invoice_id: str) -> int | None:
        """
        Synchronize a Django invoice to Odoo using the custom addon API.

        Args:
            invoice_id: External ID of the Django invoice

        Returns:
            Odoo invoice ID if successful, None otherwise
        """
        invoice = Invoice.objects.select_related("facility", "patient").get(external_id=invoice_id)

        # Prepare partner data
        partner_data = PartnerData(
            name=invoice.patient.name,
            x_care_id=str(invoice.patient.external_id),
            partner_type=PartnerType.person,
            phone=invoice.patient.phone_number,
            state="kerala",
            email="",
            agent=False,
        )

        # Prepare invoice items
        invoice_items = []
        for charge_item in ChargeItem.objects.filter(paid_invoice=invoice).select_related("charge_item_definition"):
            if charge_item.charge_item_definition:
                base_price = get_base_price_from_charge_item(charge_item, raise_if_not_found=True)
                purchase_price = get_purchase_price_from_charge_item(charge_item)
                taxes = []
                for tax in get_taxes_from_definition(charge_item.charge_item_definition):
                    taxes.append(
                        TaxData(
                            tax_name=tax["code"]["display"],
                            tax_percentage=float(tax["factor"]),
                        )
                    )
                product_data = ProductData(
                    product_name=f"CARE: {charge_item.charge_item_definition.title}",
                    x_care_id=str(charge_item.charge_item_definition.external_id),
                    mrp=float(base_price),
                    cost=float(purchase_price),
                    category=CategoryData(
                        category_name=charge_item.charge_item_definition.category.title,
                        parent_x_care_id=str(charge_item.charge_item_definition.category.parent.external_id)
                        if charge_item.charge_item_definition.category.parent
                        else "",
                        x_care_id=str(charge_item.charge_item_definition.category.external_id),
                    ),
                    status=charge_item.charge_item_definition.status,
                    taxes=taxes,
                )

                # Get all discounts if available
                discounts = get_all_discounts(charge_item)

                item = InvoiceItem(
                    product_data=product_data,
                    quantity=str(charge_item.quantity),
                    sale_price=str(base_price),
                    x_care_id=str(charge_item.external_id),
                    discounts=discounts,
                )

                if charge_item.service_resource == ChargeItemResourceOptions.service_request.value:
                    service_request = ServiceRequest.objects.get(external_id=charge_item.service_resource_id)
                    requester = service_request.requester
                elif charge_item.service_resource == ChargeItemResourceOptions.appointment.value:
                    token_booking = TokenBooking.objects.get(external_id=charge_item.service_resource_id)
                    requester = token_booking.token_slot.resource.user
                elif charge_item.service_resource == ChargeItemResourceOptions.medication_dispense.value:
                    medication_dispense = MedicationDispense.objects.get(external_id=charge_item.service_resource_id)
                    requester = (
                        medication_dispense.authorizing_request.requester
                        if medication_dispense.authorizing_request
                        else None
                    )
                else:
                    requester = None

                if requester:
                    item.agent_id = str(requester.external_id)
                invoice_items.append(item)

        logger.info("Invoice Items: %s", invoice_items)
        data = AccountMoveApiRequest(
            partner_data=partner_data,
            invoice_items=invoice_items,
            invoice_date=invoice.created_date.strftime("%d-%m-%Y"),
            x_care_id=str(invoice.external_id),
            bill_type=BillType.customer,
            due_date=invoice.created_date.strftime("%d-%m-%Y"),
            reason="",
        ).model_dump()
        logger.info("Odoo Invoice Data: %s", data)

        response = OdooConnector.call_api("api/account/move", data)
        invoice_number = response.get("invoice", {}).get("name")
        invoice.number = invoice_number
        invoice.save(update_fields=["number"])
        return response["invoice"]["id"]

    def sync_invoice_return_to_odoo_api(self, invoice_id: str) -> int | None:
        """
        Synchronize a cancelled Django invoice to Odoo using the custom addon API.

        Args:
            invoice_id: External ID of the Django invoice

        Returns:
            Odoo invoice ID if successful, None otherwise
        """
        invoice = Invoice.objects.select_related("facility", "patient").get(external_id=invoice_id)

        data = AccountMoveReturnApiRequest(
            x_care_id=str(invoice.external_id),
            reason=invoice.status,
        ).model_dump()

        logger.info("Odoo Invoice Return Data: %s", data)
        response = OdooConnector.call_api("api/account/move/return", data)
        return response["invoice"]["id"]
