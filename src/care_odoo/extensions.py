from care.emr.extensions.base import PlugExtension, ExtensionResource
from care.emr.registries.extensions.registry import ExtensionRegistry


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


ExtensionRegistry.register(SupplyDeliveryOrderExtension())


class AccountExtension(PlugExtension):
    extension_name = "account_extension"
    extension_version = "1.0.0"
    resource_type = ExtensionResource.account
    write_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Autocomplete Test Schema",
        "description": "Example schema demonstrating autocomplete field usage",
        "type": "object",
        "properties": {
            "insurance_company": {
                "type": "string",
                "title": "Insurance Company",
                "description": "Select an insurance company to be mapped to the account",
                "x-ui": {
                    "control": "autocomplete",
                    "metadata": {
                        "url": "/api/care_odoo/insurance-company",
                        "searchParam": "search_key",
                        "valueField": "id",
                        "labelField": "name",
                        "sendToken": "true",
                    },
                },
            },
        },
        "additionalProperties": "false",
    }


ExtensionRegistry.register(AccountExtension())


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
        },
        "if": {
            "properties": {
                "is_credit": {"const": True},
            },
        },
        "then": {
            "required": ["payment_method_line_id"],
            "properties": {
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
        },
        "additionalProperties": False,
    }


ExtensionRegistry.register(PaymentReconciliationExtension())
