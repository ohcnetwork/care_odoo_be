"""Effective purchase cost per base unit for one Care receipt line."""

from decimal import Decimal, InvalidOperation

ZERO = Decimal(0)
HUNDRED = Decimal(100)


def _decimal(value):
    if value in (None, ""):
        return ZERO
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _closer(candidates, anchor):
    """The candidate rate nearest the batch's own per-unit price, by ratio."""
    return min(candidates, key=lambda item: max(item[0], anchor) / min(item[0], anchor))


def care_unit_cost(line):
    """
    Cost per base unit after free packs and percentage discount, excluding tax.

    total_purchase_price is normally the price of one pack, with free_quantity
    counting packs inside supplied_item_pack_quantity, as the Odoo connector
    bills them. Some lines (mostly the January opening load) store the line
    total instead, with or without a pack size. The reading closest to the
    batch's own per-unit purchase_price wins.
    Returns (unit_cost, source); unit_cost is None when it cannot be derived.
    """
    quantity = line["supplied_item_quantity"]
    if not quantity or quantity <= 0:
        return None, "no_quantity"
    extension = (line["extensions"] or {}).get("supply_delivery_extension") or {}
    free_packs = _decimal(extension.get("free_quantity"))
    discount = _decimal(extension.get("purchase_discount"))
    if free_packs is None or free_packs < 0 or discount is None or not ZERO <= discount <= HUNDRED:
        return None, "invalid_offer"
    keep = 1 - discount / HUNDRED
    pack_size = line["supplied_item_pack_size"] or 1
    price = line["total_purchase_price"]
    anchor = line["product_purchase_price"]
    if price is None or price <= 0:
        if anchor is None:
            return None, "missing_price"
        rate, source = anchor, "care_product"
    else:
        rate, source = price / pack_size, "care_receipt"
        if anchor and anchor > 0:
            line_total = (price / quantity, "care_line_total")
            rate, source = _closer([(rate, source), line_total], anchor)
        if source == "care_line_total":
            return rate * keep, source
    billed = quantity - free_packs * pack_size
    if billed < 0:
        return None, "invalid_offer"
    return rate * billed * keep / quantity, source
