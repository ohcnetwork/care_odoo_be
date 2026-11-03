from decimal import Decimal

from care.emr.models.supply_delivery import SupplyDelivery

from care_odoo.resources.account_move.spec import CarePurchaseData


def purchase_metadata(receipt: SupplyDelivery) -> CarePurchaseData | None:
    """Describe the batch and unit sent on this receipt's vendor-bill line."""
    product = receipt.supplied_item
    if not product or not product.product_knowledge or not product.charge_item_definition:
        return None
    unit = product.product_knowledge.base_unit or {}
    if not (unit.get("code") or unit.get("display")):
        return None
    # The billing sync sends packs when present, otherwise the base quantity.
    pack_size = receipt.supplied_item_pack_size if receipt.supplied_item_pack_quantity else 1
    if not pack_size or pack_size <= 0:
        return None
    batch = product.batch or {}
    return CarePurchaseData(
        facility_uuid=product.facility.external_id,
        item_uuid=product.product_knowledge.external_id,
        item_name=product.product_knowledge.name,
        product_id=product.pk,
        product_uuid=product.external_id,
        batch=str(batch.get("lot_number", "")) if isinstance(batch, dict) else str(batch),
        charge_definition_uuid=product.charge_item_definition.external_id,
        pack_size=pack_size,
        base_unit=unit,
        received_base_units=Decimal(str(receipt.supplied_item_quantity or 0)),
    )
