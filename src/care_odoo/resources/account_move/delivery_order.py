import logging
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import ValidationError

from care.emr.models.supply_delivery import DeliveryOrder, SupplyDelivery
from care.emr.resources.inventory.supply_delivery.spec import (
    SupplyDeliveryStatusOptions,
)
from care.emr.resources.location.spec import InventoryUsage

from care_odoo.connector.connector import OdooConnectionError, OdooConnector
from care_odoo.resources.account_move.spec import (
    AccountMoveApiRequest,
    BillType,
    InvoiceItem,
    VendorBillApiRequest,
)
from care_odoo.resources.product_category.spec import CategoryData
from care_odoo.resources.product_product.spec import ProductData, TaxData
from care_odoo.resources.res_partner.spec import PartnerData, PartnerType
from care_odoo.resources.utils import (
    format_datetime_to_local_date,
    get_base_price_from_definition,
    get_purchase_price_from_definition,
    get_taxes_from_definition,
)
from care_odoo.settings import plugin_settings

logger = logging.getLogger(__name__)


def is_purchase_return(delivery_order: DeliveryOrder) -> bool:
    """An internal transfer to a supplier location that names the supplier the stock goes back to."""
    return bool(
        delivery_order.origin_id
        and delivery_order.supplier_id
        and delivery_order.destination.inventory_usage == InventoryUsage.supplier.value
    )


def batch_pack_sizes(products) -> dict[int, int]:
    """
    Units in one pack of each batch, by product ID, as the supplier billed it: the pack size of the batch's first
    receipt from a supplier, else the product's standard pack size, else 1.
    """
    sizes = {}
    receipts = (
        SupplyDelivery.objects.filter(
            supplied_item__in=products,
            order__origin__isnull=True,
            order__patient__isnull=True,
            supplied_item_pack_size__gt=0,
        )
        .order_by("id")
        .values_list("supplied_item_id", "supplied_item_pack_size")
    )
    for product_id, pack_size in receipts:
        sizes.setdefault(product_id, pack_size)
    for product in products:
        if product.id not in sizes:
            sizes[product.id] = product.standard_pack_size if (product.standard_pack_size or 0) > 0 else 1
    return sizes


def plain_number(value: Decimal) -> str:
    """A quantity without trailing zeros or exponent, e.g. Decimal("60.000000") → "60"."""
    text = f"{Decimal(value):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def supplier_partner_data(supplier) -> PartnerData:
    """The Odoo vendor for a Care supplier organization."""
    supplier_metadata = supplier.metadata or {}
    return PartnerData(
        name=supplier.name,
        x_care_id=str(supplier.external_id),
        partner_type=PartnerType.company,
        phone=supplier_metadata.get("phone", ""),
        state="kerala",
        email=supplier_metadata.get("email", ""),
        agent=False,
    )


def batch_product_data(product) -> ProductData:
    """The Odoo product for a Care batch, which Odoo finds by its charge item definition."""
    charge_item_def = product.charge_item_definition
    if charge_item_def.category:
        category_data = CategoryData(
            category_name=charge_item_def.category.title,
            parent_x_care_id=str(charge_item_def.category.parent.external_id)
            if charge_item_def.category.parent
            else "",
            x_care_id=str(charge_item_def.category.external_id),
        )
    else:
        category_data = CategoryData(
            category_name="Uncategorized",
            parent_x_care_id="",
            x_care_id="",
        )

    taxes = []
    for tax in get_taxes_from_definition(charge_item_def):
        taxes.append(
            TaxData(
                tax_name=tax["code"]["display"],
                tax_percentage=float(tax["factor"]),
            )
        )

    return ProductData(
        product_name=f"{charge_item_def.title}",
        x_care_id=str(charge_item_def.external_id),
        mrp=float(get_base_price_from_definition(charge_item_def)),
        cost=float(product.purchase_price or 0),
        category=category_data,
        status=charge_item_def.status,
        hsn=product.product_knowledge.alternate_identifier
        if product.product_knowledge and product.product_knowledge.alternate_identifier
        else "",
        taxes=taxes,
    )


class OdooDeliveryOrderResource:
    def _is_excluded(self, delivery_order: DeliveryOrder) -> bool:
        """Orders from internal suppliers aren't billed in Odoo."""
        supplier = delivery_order.supplier
        if supplier and str(supplier.external_id) in plugin_settings.CARE_ODOO_INTERNAL_SUPPLIER_ID:
            logger.info(
                "Skipping Odoo sync for delivery order %s: supplier %s is excluded",
                delivery_order.external_id,
                supplier.external_id,
            )
            return True
        return False

    def sync_delivery_order_to_odoo_api(self, delivery_order_id: str) -> int | None:
        """
        Synchronize a Django delivery order to Odoo as a vendor bill using the custom addon API.
        If the order was completed before and moved back to draft, Odoo updates its draft bill instead.

        Args:
            delivery_order_id: External ID of the Django delivery order

        Returns:
            Odoo invoice ID if successful, None otherwise
        """
        delivery_order = DeliveryOrder.objects.select_related("supplier", "destination", "destination__facility").get(
            external_id=delivery_order_id
        )

        if self._is_excluded(delivery_order):
            return None

        partner_data = supplier_partner_data(delivery_order.supplier)

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
                quantity = supply_delivery.supplied_item_pack_quantity or supply_delivery.supplied_item_quantity or 0
                total_purchase_price = supply_delivery.total_purchase_price or 0

                item = InvoiceItem(
                    product_data=batch_product_data(supply_delivery.supplied_item),
                    quantity=str(quantity),
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
        parsed_bill_date = parse_datetime(vendor_bill_date) if vendor_bill_date else None
        formatted_bill_date = format_datetime_to_local_date(parsed_bill_date, "%d-%m-%Y") if parsed_bill_date else None
        data = AccountMoveApiRequest(
            partner_data=partner_data,
            invoice_items=invoice_items,
            invoice_date=format_datetime_to_local_date(delivery_order.created_date, "%d-%m-%Y"),
            x_care_id=str(delivery_order.external_id),
            bill_type=BillType.vendor,
            due_date=format_datetime_to_local_date(delivery_order.created_date, "%d-%m-%Y"),
            reason="",
            x_created_by=delivery_order.updated_by.full_name if delivery_order.updated_by else None,
            payment_reference=(delivery_order.extensions or {}).get("payment_reference", ""),
            bill_number=delivery_order_extension.get("vendor_bill_number"),
            bill_date=formatted_bill_date,
            grn_sequence_code=plugin_settings.CARE_ODOO_GRN_SEQUENCE_MAP.get(
                str(delivery_order.destination.external_id)
            ),
        ).model_dump()

        logger.info("Odoo Delivery Order Data: %s", data)

        response = OdooConnector.call_api("api/account/move", data)
        grn_number = response.get("invoice", {}).get("grn_number")
        if grn_number:
            delivery_order.name = grn_number
            delivery_order.save(update_fields=["name"])
        return response["invoice"]["id"]

    def sync_purchase_return_to_odoo_api(self, delivery_order_id: str) -> int | None:
        """
        Create a draft supplier credit note in Odoo for a purchase return its sender has approved.

        Suppliers credit whole packs (strips) at the pack price, so each item still on the order goes on in packs,
        at its batch's purchase price (the charge item definition's when the batch has none) × the pack size. An
        item that isn't a whole number of packs refuses the approval. Odoo adds the product's purchase taxes. The
        credit note stays in draft for finance to check against the supplier's own and post. care_fe can't move an
        approved transfer back to draft or cancel it, so Care never changes the credit note afterwards.

        Returns:
            Odoo credit note ID, or None when nothing was sent
        """
        delivery_order = DeliveryOrder.objects.select_related("supplier", "updated_by").get(
            external_id=delivery_order_id
        )
        if self._is_excluded(delivery_order):
            return None

        invoice_items, part_packs = [], []
        supply_deliveries = (
            SupplyDelivery.objects.filter(
                order=delivery_order,
                status__in=[SupplyDeliveryStatusOptions.in_progress.value, SupplyDeliveryStatusOptions.completed.value],
            )
            .select_related(
                "supplied_item__charge_item_definition__category__parent",
                "supplied_item__product_knowledge",
                "supplied_inventory_item__product__charge_item_definition__category__parent",
                "supplied_inventory_item__product__product_knowledge",
            )
            .order_by("id")
        )
        # A transfer moves a batch held at the origin
        returned = [
            (
                supply_delivery,
                supply_delivery.supplied_item
                or (
                    supply_delivery.supplied_inventory_item.product if supply_delivery.supplied_inventory_item else None
                ),
            )
            for supply_delivery in supply_deliveries
        ]
        pack_sizes = batch_pack_sizes([product for _supply_delivery, product in returned if product])
        for supply_delivery, product in returned:
            if not product or not product.charge_item_definition:
                logger.warning(
                    "Purchase return %s: item %s has no charge item definition, so it is left off the credit note",
                    delivery_order_id,
                    supply_delivery.external_id,
                )
                continue
            pack_size = pack_sizes[product.id]
            packs, loose = divmod(supply_delivery.supplied_item_quantity or Decimal(0), pack_size)
            if loose:
                part_packs.append(
                    f"{product.product_knowledge.name} (batch {(product.batch or {}).get('lot_number', '-')}): "
                    f"{plain_number(supply_delivery.supplied_item_quantity)} is not a whole number of packs "
                    f"of {pack_size}"
                )
                continue
            unit_price = product.purchase_price or Decimal(
                str(get_purchase_price_from_definition(product.charge_item_definition) or 0)
            )
            invoice_items.append(
                InvoiceItem(
                    product_data=batch_product_data(product),
                    quantity=plain_number(packs),
                    sale_price=str((unit_price * pack_size).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                    x_care_id=str(supply_delivery.external_id),
                )
            )

        if part_packs:
            raise ValidationError(
                "Suppliers credit whole packs only. Change these to whole packs before approving: "
                + "; ".join(part_packs)
            )
        if not invoice_items:
            logger.info("No credit note items found for purchase return %s", delivery_order_id)
            return None

        returned_on = format_datetime_to_local_date(timezone.now(), "%d-%m-%Y")
        data = AccountMoveApiRequest(
            partner_data=supplier_partner_data(delivery_order.supplier),
            invoice_items=invoice_items,
            invoice_date=returned_on,
            due_date=returned_on,
            x_care_id=str(delivery_order.external_id),
            bill_type=BillType.vendor,
            is_refund=True,
            reason="",
            x_identifier=delivery_order.name,
            x_created_by=delivery_order.updated_by.full_name if delivery_order.updated_by else None,
        ).model_dump()
        logger.info("Odoo Purchase Return Data: %s", data)

        try:
            response = OdooConnector.call_api("api/account/move", data)
        except OdooConnectionError:
            raise
        except ValidationError as error:
            # An approval Care rolled back, e.g. after a timeout, may already have created it
            if "already exists" in str(error.detail):
                logger.warning("Odoo already has the credit note for purchase return %s", delivery_order_id)
                return None
            raise
        return response["invoice"]["id"]

    def sync_delivery_order_reopen_to_odoo_api(self, delivery_order_id: str) -> None:
        """
        Tell Odoo that a completed delivery order was moved back to draft, so more items can be added.
        Odoo refuses if the order's vendor bill is already posted, which blocks the move in Care.
        """
        delivery_order = DeliveryOrder.objects.select_related("supplier").get(external_id=delivery_order_id)
        if not self._is_excluded(delivery_order):
            self._call_vendor_bill_api("reopen", delivery_order)

    def sync_delivery_order_cancel_to_odoo_api(self, delivery_order_id: str, status: str) -> None:
        """
        Cancel the vendor bill of a cancelled delivery order in Odoo, if the order ever had one.
        Odoo refuses if the bill is already posted, which blocks the cancellation in Care.
        """
        delivery_order = DeliveryOrder.objects.select_related("supplier").get(external_id=delivery_order_id)
        if not self._is_excluded(delivery_order):
            self._call_vendor_bill_api(
                "cancel", delivery_order, f"the delivery order was marked {status.replace('_', ' ')}"
            )

    def _call_vendor_bill_api(self, action: str, delivery_order: DeliveryOrder, reason: str | None = None) -> None:
        data = VendorBillApiRequest(x_care_id=str(delivery_order.external_id), reason=reason).model_dump()
        logger.info("Odoo Vendor Bill %s Data: %s", action, data)
        OdooConnector.call_api(f"api/account/move/vendor-bill/{action}", data)
