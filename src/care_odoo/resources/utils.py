"""
Utility functions for extracting price components from charge items and charge item definitions.
"""

from care.emr.models.charge_item import ChargeItem
from care.emr.models.charge_item_definition import ChargeItemDefinition
from care.emr.resources.common.monetary_component import MonetaryComponentType
from rest_framework.exceptions import ValidationError

from care_odoo.resources.account_move.spec import (
    DiscountGroup,
    DiscountType,
    InvoiceDiscounts,
)


def get_base_price_from_components(price_components: list | None) -> str:
    """
    Extract base price from price components.

    Args:
        price_components: List of price component dictionaries, or None

    Returns:
        Base price as string, "0" if not found or None
    """
    if not price_components:
        return "0"
    for item in price_components:
        if item.get("monetary_component_type") == MonetaryComponentType.base.value:
            return item.get("amount", "0")
    return "0"


def get_base_price_from_charge_item(charge_item: ChargeItem | None, raise_if_not_found: bool = False) -> str:
    """
    Extract base price from charge item's unit price components.

    Args:
        charge_item: ChargeItem instance, or None
        raise_if_not_found: If True, raise exception when base price not found

    Returns:
        Base price as string, "0" if not found (unless raise_if_not_found=True)
    """
    if not charge_item:
        return "0"
    price = get_base_price_from_components(charge_item.unit_price_components)
    if raise_if_not_found and price == "0":
        raise Exception("Base price not found")
    return price


def get_base_price_from_definition(charge_item_definition: ChargeItemDefinition | None) -> str:
    """
    Extract base price from charge item definition's price components.

    Args:
        charge_item_definition: ChargeItemDefinition instance, or None

    Returns:
        Base price as string, "0" if not found or None
    """
    if not charge_item_definition:
        return "0"
    return get_base_price_from_components(charge_item_definition.price_components)


def get_purchase_price_from_components(price_components: list | None) -> str:
    """
    Extract purchase price from price components.

    Args:
        price_components: List of price component dictionaries, or None

    Returns:
        Purchase price as string, "0" if not found or None
    """
    if not price_components:
        return "0"
    for item in price_components:
        if (
            item.get("monetary_component_type") == MonetaryComponentType.informational.value
            and item.get("code", {}).get("code") == "purchase_price"
        ):
            return item.get("amount", "0")
    return "0"


def get_purchase_price_from_charge_item(charge_item: ChargeItem | None) -> str:
    """
    Extract purchase price from charge item's unit price components.

    Args:
        charge_item: ChargeItem instance, or None

    Returns:
        Purchase price as string, "0" if not found or None
    """
    if not charge_item:
        return "0"
    return get_purchase_price_from_components(charge_item.unit_price_components)


def get_purchase_price_from_definition(charge_item_definition: ChargeItemDefinition | None) -> str:
    """
    Extract purchase price from charge item definition's price components.

    Args:
        charge_item_definition: ChargeItemDefinition instance, or None

    Returns:
        Purchase price as string, "0" if not found or None
    """
    if not charge_item_definition:
        return "0"
    return get_purchase_price_from_components(charge_item_definition.price_components)


def get_mrp_from_components(price_components: list | None) -> str:
    """
    Extract MRP from price components.

    Args:
        price_components: List of price component dictionaries, or None

    Returns:
        MRP as string, "0" if not found or None
    """
    if not price_components:
        return "0"
    for item in price_components:
        if (
            item.get("monetary_component_type") == MonetaryComponentType.informational.value
            and item.get("code", {}).get("code") == "mrp"
        ):
            return item.get("amount", "0")
    return "0"


def get_mrp_from_charge_item(charge_item: ChargeItem | None) -> str:
    """
    Extract MRP from charge item's unit price components.

    Args:
        charge_item: ChargeItem instance, or None

    Returns:
        MRP as string, "0" if not found or None
    """
    if not charge_item:
        return "0"
    return get_mrp_from_components(charge_item.unit_price_components)


def get_mrp_from_definition(charge_item_definition: ChargeItemDefinition | None) -> str:
    """
    Extract MRP from charge item definition's price components.

    Args:
        charge_item_definition: ChargeItemDefinition instance, or None

    Returns:
        MRP as string, "0" if not found or None
    """
    if not charge_item_definition:
        return "0"
    return get_mrp_from_components(charge_item_definition.price_components)


def get_taxes_from_components(price_components: list | None) -> list:
    """
    Extract taxes from price components.

    Args:
        price_components: List of price component dictionaries, or None

    Returns:
        List of tax component dictionaries (empty list if None or not found)
    """
    taxes = []
    if not price_components:
        return taxes
    for item in price_components:
        if item.get("monetary_component_type") == MonetaryComponentType.tax.value:
            taxes.append(item)
    return taxes


def get_taxes_from_definition(charge_item_definition: ChargeItemDefinition | None) -> list:
    """
    Extract taxes from charge item definition's price components.

    Args:
        charge_item_definition: ChargeItemDefinition instance, or None

    Returns:
        List of tax component dictionaries (empty list if None or not found)
    """
    if not charge_item_definition:
        return []
    return get_taxes_from_components(charge_item_definition.price_components)


def get_all_discounts(charge_item: ChargeItem) -> list[InvoiceDiscounts] | None:
    """
    Extract all discounts from charge item's unit and total price components.

    Args:
        charge_item: ChargeItem instance

    Returns:
        List of InvoiceDiscounts if discounts found, None otherwise

    Raises:
        ValidationError: If more than 1 discount is found per item
    """
    if not charge_item or not charge_item.unit_price_components:
        return None

    # Find all discounts in unit_price_components
    unit_discounts = []
    for component in charge_item.unit_price_components:
        if component.get("monetary_component_type") == MonetaryComponentType.discount.value:
            unit_discounts.append(component)

    if not unit_discounts:
        return None

    if len(unit_discounts) > 1:
        raise ValidationError(f"More than 1 discount per item is not allowed. Found {len(unit_discounts)} discounts.")

    discounts = []
    for unit_discount in unit_discounts:
        code = unit_discount.get("code", {})
        discount_name = code.get("display")
        discount_code = code.get("code")

        # Create discount group
        discount_group = DiscountGroup(x_care_id=discount_code, name=discount_name)

        # Get discount type and rate from unit_price_components
        if unit_discount.get("factor") is not None:
            discount_type = DiscountType.factor
            rate = float(unit_discount.get("factor", 0.0))
        else:
            discount_type = DiscountType.amount
            rate = float(unit_discount.get("amount", 0.0))

        # Get discount amount from total_price_components
        disc_amt = 0.0
        if charge_item.total_price_components:
            for component in charge_item.total_price_components:
                if (
                    component.get("monetary_component_type") == MonetaryComponentType.discount.value
                    and component.get("code", {}).get("code") == discount_code
                ):
                    disc_amt = float(component.get("amount", 0.0))
                    break

        discounts.append(
            InvoiceDiscounts(
                name=discount_name,
                discount_group=discount_group,
                discount_type=discount_type,
                rate=rate,
                disc_amt=disc_amt,
            )
        )

    return discounts if discounts else None
