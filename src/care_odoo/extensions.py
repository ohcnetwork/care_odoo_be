from care.emr.extensions.base import PlugExtension, ExtensionResource
from care.emr.registries.extensions.registry import ExtensionRegistry

from care.emr.models import SupplyDelivery
from care.utils.rounding.rounding import care_round


class SupplyDeliveryExtension(PlugExtension):
    extension_name = "supply_delivery_extension"
    extension_version = "1.0.0"
    resource_type = ExtensionResource.supply_delivery
    write_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Free Quantity",
        "type": "object",
        "properties": {
            "free_quantity": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "title": "Free quantity",
                "description": "items received",
                "x-ui": {"control": "textbox"},
            },
            "purchase_discount": {
                "type": "number",
                "minimum": 0.00,
                "default": 0.00,
                "title": "Purchase Discount",
                "x-ui": {"control": "textbox"},
            },
        },
        "additionalProperties": "false",
    }


ExtensionRegistry.register(SupplyDeliveryExtension())


def compute_charge_item_components(charge_item_definition):
    from care.emr.models.resource_category import merge_monetary_components
    from care.emr.resources.charge_item.apply_charge_item_definition import compute_global_components

    price_components = charge_item_definition.price_components
    if charge_item_definition.category:
        price_components = merge_monetary_components(
            charge_item_definition.category.calculated_monetary_components,
            price_components,
        )
    price_components = compute_global_components(
        charge_item_definition, price_components
    )
    return price_components

def calculate_amount(component, quantity, base):
    from care.utils.rounding.covert_type import convert_to_decimal

    if component.get("amount"):
        return care_round(convert_to_decimal(component.get("amount")) * quantity)
    if component.get("factor"):
        return care_round(base * convert_to_decimal(component.get("factor")) / 100)
    return 0


class SupplyDeliveryOrderExtension(PlugExtension):
    extension_name = "supply_delivery_order_extension"
    extension_version = "1.0.0"
    resource_type = ExtensionResource.supply_delivery_order
    write_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Purchase Delivery Details",
        "type": "object",
        "x-ui": {"control": "grid"},
        "properties": {
            "vendor_bill_number": {"type": "string", "title": "Vendor Bill Number", "x-ui": {"control": "textbox"}},
            "vendor_bill_date": {
                "type": "string",
                "format": "date",
                "title": "Vendor Bill Date",
                "x-ui": {"control": "date"},
            },
            "total_discount": {
                "type": "number",
                "minimum": 0.00,
                "default": 0.00,
                "title": "Total Discount",
                "x-ui": {"control": "textbox"},
            },
        },
        "additionalProperties": "false",
    }
    retrieve_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Purchase Delivery Details",
        "type": "object",
        "x-ui": {"control": "grid"},
        "properties": {
            "vendor_bill_number": {"type": "string", "title": "Vendor Bill Number", "x-ui": {"control": "textbox"}},
            "vendor_bill_date": {
                "type": "string",
                "format": "date",
                "title": "Vendor Bill Date",
                "x-ui": {"control": "date"},
            },
            "total_discount": {
                "type": "number",
                "minimum": 0.00,
                "default": 0.00,
                "title": "Total Discount",
                "x-ui": {"control": "textbox"},
            },
            "total_price": {
                "type": "number",
                "minimum": 0.00,
                "default": 0.00,
                "title": "Total Price",
            },
        },
        "additionalProperties": "false",
    }

    @staticmethod
    def _compute_total_price(data, resource):
        from decimal import Decimal

        total_price = Decimal("0")
        for item in SupplyDelivery.objects.filter(order=resource, status__in=["in_progress", "completed"]):
            pack_qty = Decimal(str(item.supplied_item_pack_quantity or 0))
            free_qty = Decimal(str(item.extensions.get("supply_delivery_extension", {}).get("free_quantity", 0)))
            unit_pack_price = Decimal(str(item.total_purchase_price or 0))
            if not item.supplied_item.standard_pack_size:
                continue
            unit_price = unit_pack_price / Decimal(str(item.supplied_item.standard_pack_size or 0))
            tax = Decimal("0")
            if not item.supplied_item.charge_item_definition:
                continue
            for component in compute_charge_item_components(item.supplied_item.charge_item_definition):
                if component.get("monetary_component_type", "") == "tax":
                    tax += calculate_amount(component, 1 , unit_price)
            total_tax = tax * item.supplied_item.standard_pack_size * (pack_qty - free_qty)
            total_price += Decimal((pack_qty - free_qty) * unit_pack_price) + total_tax
        data["total_price"] = str(care_round(Decimal(total_price), precision=2))
        return data

    def deserialize_extensions_list(self, data, resource):
        return data

    def deserialize_extensions_retrieve(self, data, resource):
        return self._compute_total_price(data, resource)

ExtensionRegistry.register(SupplyDeliveryOrderExtension())


class PaymentReconciliationExtension(PlugExtension):
    """
    Extension for PaymentReconciliation to support Credit (Care of Account) payments.

    When a payment is made using a credit source (charity, sponsor, fund),
    the payment_method_line_id identifies the specific credit source in Odoo.
    """

    extension_name = "payment_reconciliation_credit_extension"
    extension_version = "1.0.0"
    resource_type = ExtensionResource.payment_reconciliation
    write_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Credit Payment Details",
        "description": "Extension for Care of Account (credit) payments. Note: Credit payments cannot be used for refunds.",
        "type": "object",
        "properties": {
            "is_credit": {
                "type": "boolean",
                "title": "Is this a Credit payment?",
                "description": "Check if this payment is made by a charity, sponsor, or fund on behalf of the patient. Cannot be used for refunds.",
                "default": False,
                "x-ui": {"control": "checkbox"},
            },
            "payment_method_line_id": {
                "type": "string",
                "title": "Credit Source",
                "description": "Select the charity/sponsor/fund paying on behalf of the patient",
                "x-ui": {
                    "control": "autocomplete",
                    "metadata": {
                        "url": "/api/care_odoo/payment-method-line",
                        "searchParam": "search",
                        "valueField": "id",
                        "labelField": "name",
                        "sendToken": "true",
                    },
                },
            },
        },
        "if": {
            "properties": {
                "is_credit": {"const": True},
            },
        },
        "then": {
            "required": ["payment_method_line_id"],
        },
        "additionalProperties": False,
    }


ExtensionRegistry.register(PaymentReconciliationExtension())
