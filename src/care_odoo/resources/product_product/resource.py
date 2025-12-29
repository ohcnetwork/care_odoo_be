from care.emr.models.product import Product

from care_odoo.connector.connector import OdooConnector
from care_odoo.resources.product_category.spec import CategoryData
from care_odoo.resources.product_product.spec import ProductData, TaxData
from care_odoo.resources.utils import (
    get_base_price_from_definition,
    get_purchase_price_from_definition,
    get_taxes_from_definition,
)


class OdooProductProductResource:
    def sync_product_to_odoo_api(self, charge_item_definition, hsn: str = "") -> int | None:
        """
        Synchronize a charge item definition to Odoo as a product.

        Args:
            charge_item_definition: ChargeItemDefinition instance

        Returns:
            Odoo product ID if successful, None otherwise
        """
        base_price = get_base_price_from_definition(charge_item_definition)
        purchase_price = get_purchase_price_from_definition(charge_item_definition)

        taxes = []
        for tax in get_taxes_from_definition(charge_item_definition):
            taxes.append(
                TaxData(
                    tax_name=tax["code"]["display"],
                    tax_percentage=float(tax["factor"]),
                )
            )
        data = ProductData(
            product_name=f"{charge_item_definition.title}",
            x_care_id=str(charge_item_definition.external_id),
            mrp=float(base_price),
            cost=float(purchase_price),
            category=CategoryData(
                category_name=charge_item_definition.category.title,
                parent_x_care_id=str(charge_item_definition.category.parent.external_id)
                if charge_item_definition.category.parent
                else "",
                x_care_id=str(charge_item_definition.category.external_id),
            ),
            taxes=taxes,
            hsn=hsn,
            status=charge_item_definition.status,
        ).model_dump()

        response = OdooConnector.call_api("api/add/product", data)
        return response.get("product", {}).get("id")

    def sync_product_from_product_model(self, product: Product) -> int | None:
        """
        Synchronize a product to Odoo if it has a charge item definition.

        Args:
            product: Product instance

        Returns:
            Odoo product ID if successful, None otherwise
        """
        if not product.charge_item_definition:
            return None

        hsn = (
            product.product_knowledge.alternate_identifier
            if product.product_knowledge and product.product_knowledge.alternate_identifier
            else ""
        )

        return self.sync_product_to_odoo_api(product.charge_item_definition, hsn)
