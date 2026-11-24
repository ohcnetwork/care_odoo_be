from decimal import Decimal
from unittest import mock

from care.emr.models.charge_item_definition import ChargeItemDefinition
from care.emr.models.inventory_item import InventoryItem
from care.emr.models.location import FacilityLocation
from care.emr.models.organization import Organization
from care.emr.models.product import Product
from care.emr.models.supply_delivery import DeliveryOrder, SupplyDelivery
from care.utils.tests.base import CareAPITestBase
from django.utils import timezone
from model_bakery import baker
from rest_framework.exceptions import ValidationError

from care_odoo.settings import plugin_settings

ACCOUNT_MOVE = "api/account/move"


class SupplierOrderTestBase(CareAPITestBase):
    """A pharmacy, a supplier location for returns, a supplier and one batch; Odoo is faked."""

    def setUp(self):
        self.odoo_error = None
        patcher = mock.patch("care_odoo.connector.connector.OdooConnector.call_api", side_effect=self.fake_odoo)
        self.call_api = patcher.start()
        self.addCleanup(patcher.stop)

        self.facility = self.create_facility(self.create_user())
        self.pharmacy = baker.make(FacilityLocation, facility=self.facility, inventory_usage="store")
        self.returns = baker.make(
            FacilityLocation, facility=self.facility, inventory_usage="supplier", name="Stock Purchase Return"
        )
        self.supplier = baker.make(
            Organization,
            org_type="product_supplier",
            name="Johal and Sons",
            metadata={"phone": "+919800000000", "email": "accounts@johal.example"},
        )
        self.category = baker.make(
            "emr.ResourceCategory",
            facility=self.facility,
            resource_type="charge_item_definition",
            title="Medicines 5%",
            parent=None,
        )
        self.definition = baker.make(
            ChargeItemDefinition,
            facility=self.facility,
            category=self.category,
            title="Amoxicillin 500mg Capsule",
            status="active",
            price_components=[
                {"monetary_component_type": "base", "amount": "18"},
                {
                    "monetary_component_type": "informational",
                    "code": {"code": "purchase_price"},
                    "amount": "11",
                },
                {
                    "monetary_component_type": "tax",
                    "code": {"code": "cgst", "display": "CGST 2.5%"},
                    "factor": 2.5,
                },
                {
                    "monetary_component_type": "tax",
                    "code": {"code": "sgst", "display": "SGST 2.5%"},
                    "factor": 2.5,
                },
            ],
        )
        # A strip of 10 capsules at ₹12.50 each
        self.batch = self.make_batch(Decimal("12.5"), pack_size=10)

    def fake_odoo(self, endpoint, data, *args, **kwargs):
        if endpoint != ACCOUNT_MOVE:
            return {}
        if self.odoo_error:
            raise ValidationError(self.odoo_error)
        return {"invoice": {"id": 77, "name": "/"}}

    def make_batch(self, purchase_price, definition="default", pack_size=None):
        return baker.make(
            Product,
            facility=self.facility,
            product_knowledge=baker.make(
                "emr.ProductKnowledge", facility=self.facility, name="AMOXICILLIN 500", alternate_identifier="30041010"
            ),
            charge_item_definition=self.definition if definition == "default" else definition,
            purchase_price=purchase_price,
            standard_pack_size=pack_size,
            batch={"lot_number": "AMX-001"},
        )

    def receive(self, batch, pack_size, pack_price=None, packs=10, supplier="default"):
        """A supplier receipt of the batch, in packs of the given size, at the given price of a pack."""
        order = baker.make(
            DeliveryOrder,
            origin=None,
            destination=self.pharmacy,
            supplier=self.supplier if supplier == "default" else supplier,
            status="draft",
        )
        return baker.make(
            SupplyDelivery,
            order=order,
            supplied_item=batch,
            supplied_item_quantity=Decimal(pack_size * packs),
            supplied_item_pack_quantity=packs,
            supplied_item_pack_size=pack_size,
            total_purchase_price=pack_price,
            status="completed",
        )

    def make_order(self, destination=None, supplier="default", status="draft"):
        return baker.make(
            DeliveryOrder,
            name="Expired, back to Johal",
            origin=self.pharmacy,
            destination=destination or self.returns,
            supplier=self.supplier if supplier == "default" else supplier,
            status=status,
        )

    def add_line(self, order, quantity, batch=None, status="in_progress"):
        held, _ = InventoryItem.objects.get_or_create(
            product=batch or self.batch,
            location=self.pharmacy,
            defaults={"status": "active", "net_content": Decimal(500)},
        )
        return baker.make(
            SupplyDelivery,
            order=order,
            supplied_inventory_item=held,
            supplied_item_quantity=Decimal(quantity),
            status=status,
        )

    def set_status(self, order, status):
        order.status = status
        order.save()

    def approve(self, order):
        self.set_status(order, "pending")

    def account_moves(self):
        return [call.args[1] for call in self.call_api.call_args_list if call.args[0] == ACCOUNT_MOVE]


class PurchaseReturnCreditNoteTests(SupplierOrderTestBase):
    """An internal transfer to a supplier location that names the supplier becomes a draft credit note in Odoo."""

    def test_approved_return_becomes_a_draft_credit_note(self):
        order = self.make_order()
        line = self.add_line(order, 20)
        self.add_line(order, 5, status="abandoned")

        self.approve(order)

        [data] = self.account_moves()
        self.assertTrue(data["is_refund"])
        self.assertEqual(data["bill_type"], "vendor")
        self.assertEqual(data["x_care_id"], str(order.external_id))
        self.assertEqual(data["x_identifier"], "Expired, back to Johal")
        self.assertEqual(data["invoice_date"], timezone.localdate().strftime("%d-%m-%Y"))
        self.assertIsNone(data["grn_sequence_code"])
        self.assertEqual(data["partner_data"]["x_care_id"], str(self.supplier.external_id))
        self.assertEqual(data["partner_data"]["name"], "Johal and Sons")
        self.assertEqual(data["partner_data"]["email"], "accounts@johal.example")

        # Only the item still on the order, as 2 strips of 10 at the batch's price × 10
        [item] = data["invoice_items"]
        self.assertEqual(item["x_care_id"], str(line.external_id))
        self.assertEqual(item["quantity"], "2")
        self.assertEqual(item["sale_price"], "125.00")
        self.assertEqual(item["free_qty"], "0.0")
        product = item["product_data"]
        self.assertEqual(product["x_care_id"], str(self.definition.external_id))
        self.assertEqual(product["product_name"], "Amoxicillin 500mg Capsule")
        self.assertEqual(product["hsn"], "30041010")
        self.assertEqual(
            product["category"],
            {"category_name": "Medicines 5%", "parent_x_care_id": "", "x_care_id": str(self.category.external_id)},
        )
        self.assertEqual(product["mrp"], 18)
        self.assertEqual(product["cost"], 12.5)
        self.assertEqual(
            [(tax["tax_name"], tax["tax_percentage"]) for tax in product["taxes"]],
            [("CGST 2.5%", 2.5), ("SGST 2.5%", 2.5)],
        )

    def test_batch_without_price_uses_the_definition_purchase_price(self):
        order = self.make_order()
        self.add_line(order, 3, batch=self.make_batch(None))

        self.approve(order)

        # No pack size anywhere, so it goes on in single units
        [data] = self.account_moves()
        [item] = data["invoice_items"]
        self.assertEqual((item["quantity"], item["sale_price"]), ("3", "11.00"))

    def test_pack_size_comes_from_the_supplier_receipt_first(self):
        # Billed in packs of 5 although the product says 10; the price rounds to the paisa
        batch = self.make_batch(Decimal("9.143"), pack_size=10)
        self.receive(batch, pack_size=5)
        order = self.make_order()
        self.add_line(order, 15, batch=batch)

        self.approve(order)

        [data] = self.account_moves()
        [item] = data["invoice_items"]
        self.assertEqual((item["quantity"], item["sale_price"]), ("3", "45.72"))

    def test_pack_price_is_what_the_supplier_billed(self):
        # ₹142.14 a strip of 10 was kept as ₹14.21 a capsule, which × 10 is ₹142.10
        batch = self.make_batch(Decimal("14.21"), pack_size=10)
        self.receive(batch, pack_size=10, pack_price=Decimal("142.14"))
        order = self.make_order()
        self.add_line(order, 20, batch=batch)

        self.approve(order)

        [data] = self.account_moves()
        [item] = data["invoice_items"]
        self.assertEqual((item["quantity"], item["sale_price"]), ("2", "142.14"))

    def test_receipts_priced_otherwise_are_passed_over(self):
        # An older price before the batch's own, and a line total from the opening stock load
        repriced = self.make_batch(Decimal("12.5"), pack_size=10)
        self.receive(repriced, pack_size=10, pack_price=Decimal(130))
        self.receive(repriced, pack_size=10, pack_price=Decimal("125.04"))
        opening = self.make_batch(Decimal("14.03"))
        self.receive(opening, pack_size=1, pack_price=Decimal("1487.18"), packs=106)
        order = self.make_order()
        self.add_line(order, 20, batch=repriced)
        self.add_line(order, 6, batch=opening)

        self.approve(order)

        [data] = self.account_moves()
        self.assertEqual(
            [(item["quantity"], item["sale_price"]) for item in data["invoice_items"]],
            [("2", "125.04"), ("6", "14.03")],
        )

    def test_receipts_from_the_returns_supplier_come_first(self):
        # Loaded as opening stock in single units at a line total, then bought from Johal in strips of 10
        opening_stock = baker.make(Organization, org_type="product_supplier", name="Opening stock")
        batch = self.make_batch(Decimal("14.21"))
        self.receive(batch, pack_size=1, pack_price=Decimal("284.20"), packs=20, supplier=opening_stock)
        self.receive(batch, pack_size=10, pack_price=Decimal("142.14"))
        to_johal = self.make_order()
        self.add_line(to_johal, 20, batch=batch)
        # Never bought from this supplier, so every receipt of the batch counts, the oldest first
        other = baker.make(Organization, org_type="product_supplier", name="Varghese & Company")
        to_other = self.make_order(supplier=other)
        self.add_line(to_other, 20, batch=batch)

        self.approve(to_johal)
        self.approve(to_other)

        to_johal_data, to_other_data = self.account_moves()
        [item] = to_johal_data["invoice_items"]
        self.assertEqual((item["quantity"], item["sale_price"]), ("2", "142.14"))
        [item] = to_other_data["invoice_items"]
        self.assertEqual((item["quantity"], item["sale_price"]), ("20", "14.21"))

    def test_part_packs_block_the_approval(self):
        order = self.make_order()
        self.add_line(order, 62)

        with self.assertRaisesMessage(
            ValidationError, "AMOXICILLIN 500 (batch AMX-001): 62 is not a whole number of packs of 10"
        ):
            self.approve(order)
        self.assertEqual(self.account_moves(), [])

    def test_only_approval_sends_it(self):
        order = self.make_order()
        self.add_line(order, 20)
        order.note = "Expired in October"
        order.save()
        self.assertEqual(self.account_moves(), [])

        self.approve(order)
        # The return location receiving it, and later edits, send nothing more
        self.set_status(order, "completed")
        order.note = "Handed to the supplier's rep"
        order.save()
        self.assertEqual(len(self.account_moves()), 1)

    def test_other_transfers_are_not_sent(self):
        ward = baker.make(FacilityLocation, facility=self.facility, inventory_usage="consumption")
        to_ward = self.make_order(destination=ward)
        no_supplier = self.make_order(supplier=None)
        for order in (to_ward, no_supplier):
            self.add_line(order, 20)
            self.approve(order)

        self.assertEqual(self.account_moves(), [])

    def test_internal_supplier_is_not_sent(self):
        order = self.make_order()
        self.add_line(order, 20)

        with mock.patch.object(plugin_settings, "CARE_ODOO_INTERNAL_SUPPLIER_ID", str(self.supplier.external_id)):
            self.approve(order)

        self.assertEqual(self.account_moves(), [])

    def test_items_without_a_charge_item_definition_are_left_off(self):
        order = self.make_order()
        self.add_line(order, 20, batch=self.make_batch(Decimal(4), definition=None))

        self.approve(order)

        self.assertEqual(self.account_moves(), [])

    def test_credit_note_already_in_odoo_approves(self):
        # An approval Care rolled back after Odoo made the credit note, e.g. on a timeout
        order = self.make_order()
        self.add_line(order, 20)
        self.odoo_error = "Invoice already exists"

        self.approve(order)

        order.refresh_from_db()
        self.assertEqual(order.status, "pending")

    def test_odoo_refusal_blocks_approval(self):
        order = self.make_order()
        self.add_line(order, 20)
        self.odoo_error = f"Product with id {self.definition.external_id} is not exists"

        with self.assertRaisesMessage(ValidationError, "is not exists"):
            self.approve(order)


class VendorBillTests(SupplierOrderTestBase):
    """Receipts from a supplier still become vendor bills, with the same product details."""

    def test_completed_receipt_becomes_a_vendor_bill(self):
        order = baker.make(
            DeliveryOrder, origin=None, destination=self.pharmacy, supplier=self.supplier, status="pending"
        )
        line = baker.make(
            SupplyDelivery,
            order=order,
            supplied_item=self.batch,
            supplied_item_quantity=Decimal(100),
            supplied_item_pack_quantity=10,
            supplied_item_pack_size=10,
            total_purchase_price=Decimal(125),
            status="completed",
        )

        self.set_status(order, "completed")

        [data] = self.account_moves()
        self.assertFalse(data["is_refund"])
        self.assertEqual(data["partner_data"]["x_care_id"], str(self.supplier.external_id))
        [item] = data["invoice_items"]
        self.assertEqual(item["x_care_id"], str(line.external_id))
        self.assertEqual((Decimal(item["quantity"]), Decimal(item["sale_price"])), (10, 125))
        self.assertEqual(item["product_data"]["x_care_id"], str(self.definition.external_id))
        self.assertEqual(item["product_data"]["cost"], 12.5)
        self.assertEqual(len(item["product_data"]["taxes"]), 2)
