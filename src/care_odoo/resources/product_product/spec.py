from decimal import Decimal
from enum import Enum

from pydantic import BaseModel

from care_odoo.resources.product_category.spec import CategoryData


class TaxData(BaseModel):
    tax_name: str
    tax_percentage: Decimal


class ProductStatus(str, Enum):
    active = "active"
    retired = "retired"
    draft = "draft"


class ProductData(BaseModel):
    product_name: str
    x_care_id: str
    cost: Decimal
    mrp: Decimal
    category: CategoryData
    taxes: list[TaxData] | None = None
    hsn: str | None = None
    status: ProductStatus
