import logging
from datetime import datetime

from care.emr.models.supply_delivery import DeliveryOrder, SupplyDelivery
from care.emr.resources.inventory.supply_delivery.spec import (
    SupplyDeliveryStatusOptions,
)

from care_odoo.connector.connector import OdooConnector
from care_odoo.resources.account_move.spec import (
    AccountMoveApiRequest,
    BillType,
    InvoiceItem,
)
from care_odoo.resources.product_category.spec import CategoryData
from care_odoo.resources.product_product.spec import ProductData, TaxData
from care_odoo.resources.res_partner.spec import PartnerData, PartnerType
from care_odoo.resources.utils import (
    get_base_price_from_definition,
    get_purchase_price_from_definition,
    get_taxes_from_definition,
)
from care_odoo.settings import plugin_settings

logger = logging.getLogger(__name__)


class OdooDeliveryOrderResource:
    def sync_delivery_order_to_odoo_api(self, delivery_order_id: str) -> int | None:
        """
        Synchronize a Django delivery order to Odoo as a vendor bill using the custom addon API.

        Args:
            delivery_order_id: External ID of the Django delivery order

        Returns:
            Odoo invoice ID if successful, None otherwise
        """
        delivery_order = DeliveryOrder.objects.select_related("supplier", "destination", "destination__facility").get(
            external_id=delivery_order_id
        )

        internal_supplier_id = plugin_settings.CARE_ODOO_INTERNAL_SUPPLIER_ID
        if str(delivery_order.supplier.external_id) in internal_supplier_id:
            logger.info(
                "Skipping Odoo sync for delivery order %s: supplier %s is excluded",
                delivery_order_id,
                delivery_order.supplier.external_id,
            )
            return None

        # Prepare partner data for supplier
        supplier_metadata = delivery_order.supplier.metadata or {}
        partner_data = PartnerData(
            name=delivery_order.supplier.name,
            x_care_id=str(delivery_order.supplier.external_id),
            partner_type=PartnerType.company,
            phone=supplier_metadata.get("phone", ""),
            state="kerala",
            email=supplier_metadata.get("email", ""),
            agent=False,
        )

        # Prepare invoice items from supply deliveries
        invoice_items = []
        supply_deliveries = SupplyDelivery.objects.filter(
            order=delivery_order, status=SupplyDeliveryStatusOptions.completed.value
        ).select_related(
            "supplied_item",
            "supplied_item__charge_item_definition",
            "supplied_item__charge_item_definition__category",
            "supplied_item__charge_item_definition__category__parent",
            "supplied_item__product_knowledge",
        )

        for supply_delivery in supply_deliveries:
            if supply_delivery.supplied_item and supply_delivery.supplied_item.charge_item_definition:
                product = supply_delivery.supplied_item
                charge_item_def = product.charge_item_definition
                base_price = get_base_price_from_definition(charge_item_def)
                quantity = supply_delivery.supplied_item_pack_quantity or supply_delivery.supplied_item_quantity or 0

                total_purchase_price = supply_delivery.total_purchase_price or 0
                item_purchase_price = supply_delivery.supplied_item.purchase_price or 0

                # Get category data if charge item definition exists
                if product.charge_item_definition and product.charge_item_definition.category:
                    category_data = CategoryData(
                        category_name=product.charge_item_definition.category.title,
                        parent_x_care_id=str(product.charge_item_definition.category.parent.external_id)
                        if product.charge_item_definition.category.parent
                        else "",
                        x_care_id=str(product.charge_item_definition.category.external_id),
                    )
                else:
                    category_data = CategoryData(
                        category_name="Uncategorized",
                        parent_x_care_id="",
                        x_care_id="",
                    )

                taxes = []
                for tax in get_taxes_from_definition(product.charge_item_definition):
                    taxes.append(
                        TaxData(
                            tax_name=tax["code"]["display"],
                            tax_percentage=float(tax["factor"]),
                        )
                    )

                product_data = ProductData(
                    product_name=f"{product.charge_item_definition.title}",
                    x_care_id=str(product.charge_item_definition.external_id),
                    mrp=float(base_price),
                    cost=float(item_purchase_price),
                    category=category_data,
                    status=product.charge_item_definition.status,
                    hsn=product.product_knowledge.alternate_identifier
                    if product.product_knowledge and product.product_knowledge.alternate_identifier
                    else "",
                    taxes=taxes,
                )

                item = InvoiceItem(
                    product_data=product_data,
                    quantity=str(
                        quantity
                    ),
                    free_qty=str(
                        ((supply_delivery.extensions or {}).get("supply_delivery_extension") or {}).get("free_quantity")
                        or 0
                    ),
                    sale_price=str(total_purchase_price),
                    x_care_id=str(supply_delivery.external_id),
                )

                invoice_items.append(item)

        logger.info("Delivery Order Items: %s", invoice_items)

        if not invoice_items:
            logger.info("No invoice items found for delivery order %s", delivery_order_id)
            return None

        # Prepare final data using our spec with vendor bill type
        delivery_order_extension = (delivery_order.extensions or {}).get("supply_delivery_order_extension", {})
        vendor_bill_date = delivery_order_extension.get("vendor_bill_date")
        formatted_bill_date = (
            datetime.fromisoformat(vendor_bill_date.replace("Z", "+00:00")).strftime("%d-%m-%Y")
            if vendor_bill_date
            else None
        )
        data = AccountMoveApiRequest(
            partner_data=partner_data,
            invoice_items=invoice_items,
            invoice_date=delivery_order.created_date.strftime("%d-%m-%Y"),
            x_care_id=str(delivery_order.external_id),
            bill_type=BillType.vendor,
            due_date=delivery_order.created_date.strftime("%d-%m-%Y"),
            reason="",
            x_created_by=delivery_order.updated_by.full_name if delivery_order.updated_by else None,
            payment_reference=(delivery_order.extensions or {}).get("payment_reference", ""),
            bill_number=delivery_order_extension.get("vendor_bill_number"),
            bill_date=formatted_bill_date,
        ).model_dump()

        logger.info("Odoo Delivery Order Data: %s", data)

        response = OdooConnector.call_api("api/account/move", data)
        return response["invoice"]["id"]
