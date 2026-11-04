import io
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from uuid import uuid4

from care.emr.models.inventory_item import InventoryItem
from care.emr.models.location import FacilityLocation
from care.emr.models.medication_dispense import MedicationDispense
from care.emr.models.product import Product
from care.emr.models.supply_delivery import DeliveryOrder, SupplyDelivery
from care.emr.resources.inventory.inventory_item.sync_inventory_item import (
    sync_inventory_item,
)
from care.utils.tests.base import CareAPITestBase
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import Sum
from django.test import TestCase
from model_bakery import baker

from care_odoo.resources.stock_valuation.costing import care_unit_cost
from care_odoo.resources.stock_valuation.valuation import value_inventory


def day(number):
    return datetime(2026, 4, number, 12, tzinfo=UTC)


class CareUnitCostTests(TestCase):
    def line(self, **overrides):
        line = {
            "supplied_item_quantity": Decimal(100),
            "supplied_item_pack_size": 10,
            "total_purchase_price": Decimal(100),
            "product_purchase_price": None,
            "extensions": {},
        }
        line.update(overrides)
        return line

    def test_free_packs_and_discount(self):
        # 10 packs of 10, 2 free, ₹100 a pack, 10% off: 8 x 100 x 0.9 / 100 units.
        cost, source = care_unit_cost(
            self.line(
                extensions={
                    "supply_delivery_extension": {
                        "free_quantity": "2",
                        "purchase_discount": "10",
                    }
                }
            )
        )
        self.assertEqual(cost, Decimal("7.2"))
        self.assertEqual(source, "care_receipt")

    def test_legacy_line_total_without_pack_size(self):
        # 100 units for ₹740 in total, against a batch price of ₹7.40 a unit.
        cost, source = care_unit_cost(
            self.line(
                supplied_item_pack_size=1,
                total_purchase_price=Decimal(740),
                product_purchase_price=Decimal("7.4"),
            )
        )
        self.assertEqual(cost, Decimal("7.4"))
        self.assertEqual(source, "care_line_total")

    def test_pack_price_kept_when_it_matches_the_batch(self):
        cost, source = care_unit_cost(self.line(product_purchase_price=Decimal(10)))
        self.assertEqual((cost, source), (Decimal(10), "care_receipt"))

    def test_falls_back_to_product_price_then_flags(self):
        cost, source = care_unit_cost(self.line(total_purchase_price=None, product_purchase_price=Decimal(6)))
        self.assertEqual((cost, source), (Decimal(6), "care_product"))
        self.assertEqual(
            care_unit_cost(self.line(total_purchase_price=None)),
            (None, "missing_price"),
        )

    def test_more_free_than_received_is_invalid(self):
        line = self.line(extensions={"supply_delivery_extension": {"free_quantity": "11"}})
        self.assertEqual(care_unit_cost(line), (None, "invalid_offer"))

    def test_negative_free_packs_are_invalid(self):
        line = self.line(extensions={"supply_delivery_extension": {"free_quantity": "-1"}})
        self.assertEqual(care_unit_cost(line), (None, "invalid_offer"))


class FifoValuationTests(CareAPITestBase):
    """Receive 100 @ ₹8 then 50 @ ₹15, use 60; the 90 left are 50 @ 15 + 40 @ 8."""

    def setUp(self):
        # Care saves here would otherwise sync users and products to Odoo. Orders stay
        # pending so no vendor bill is built; valuation reads only the line status.
        patcher = mock.patch("care_odoo.connector.connector.OdooConnector.call_api", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.facility = self.create_facility(self.create_user())
        self.store = self.location("store")
        self.pharmacy = self.location("store")
        self.ward = self.location("consumption")
        self.batch = baker.make(
            Product,
            facility=self.facility,
            product_knowledge=baker.make("emr.ProductKnowledge", facility=self.facility),
            purchase_price=Decimal(10),
        )
        patient = self.create_patient()
        self.encounter = self.create_encounter(patient, self.facility, self.facility.default_internal_organization)
        self.receive(1, 100, packs=10, pack_price=100, free=2)
        self.receive(3, 50, packs=5, pack_price=150)
        self.transfer(4, self.store, self.pharmacy, 30)
        self.transfer(5, self.store, self.ward, 40)
        self.dispense(5, self.pharmacy, 20)

    def location(self, usage):
        return baker.make(FacilityLocation, facility=self.facility, inventory_usage=usage)

    def item(self, location):
        item, _ = InventoryItem.objects.get_or_create(
            product=self.batch, location=location, defaults={"status": "active"}
        )
        return item

    def dated(self, model, instance, number):
        model.objects.filter(id=instance.id).update(created_date=day(number))

    def receive(self, number, quantity, *, packs, pack_price, free=0):
        order = baker.make(DeliveryOrder, origin=None, destination=self.store, status="pending")
        line = baker.make(
            SupplyDelivery,
            order=order,
            status="completed",
            supplied_item=self.batch,
            supplied_inventory_item=self.item(self.store),
            supplied_item_quantity=Decimal(quantity),
            supplied_item_pack_quantity=packs,
            supplied_item_pack_size=quantity // packs,
            total_purchase_price=Decimal(pack_price),
            extensions={"supply_delivery_extension": {"free_quantity": str(free)}},
        )
        self.dated(SupplyDelivery, line, number)
        return line

    def transfer(self, number, origin, destination, quantity, status="completed"):
        order = baker.make(DeliveryOrder, origin=origin, destination=destination, status="pending")
        line = baker.make(
            SupplyDelivery,
            order=order,
            status=status,
            supplied_inventory_item=self.item(origin),
            supplied_item_quantity=Decimal(quantity),
        )
        self.item(destination)
        self.dated(SupplyDelivery, line, number)

    def dispense(self, number, location, quantity):
        dispense = baker.make(
            MedicationDispense,
            item=self.item(location),
            location=location,
            encounter=self.encounter,
            patient=self.encounter.patient,
            status="completed",
            quantity=Decimal(quantity),
        )
        self.dated(MedicationDispense, dispense, number)

    def row(self, number):
        rows = value_inventory(self.facility, day(number))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_remaining_stock_uses_newest_layers(self):
        row = self.row(6)
        self.assertEqual(row["quantity"], 90)
        self.assertEqual(round(row["value"], 6), Decimal(50 * 15 + 40 * 8))
        self.assertEqual(row["flags"], "")

    def test_stock_moving_between_locations_stays_in_the_facility(self):
        self.transfer(5, self.store, self.pharmacy, 10, status="in_progress")
        row = self.row(6)
        self.assertEqual(row["quantity"], 90)
        self.assertEqual(round(row["value"], 6), Decimal(50 * 15 + 40 * 8))

    def test_as_of_date_ignores_later_movements(self):
        row = self.row(2)
        self.assertEqual(row["quantity"], 100)
        self.assertEqual(row["value"], Decimal(800))

    def test_confirmed_cost_overrides_care_price(self):
        newest = SupplyDelivery.objects.filter(order__origin__isnull=True, supplied_item_quantity=50).get()
        with TemporaryDirectory() as folder:
            path = Path(folder) / "costs.csv"
            # One Care line billed in two parts: the parts are added together.
            path.write_text(
                f"care_line_id,price_subtotal,reference\n{newest.external_id},300,B1\n{newest.external_id},200,B2\n"
            )
            call_command("import_valuation_costs", file=str(path), stdout=io.StringIO())
            output = io.StringIO()
            call_command("import_valuation_costs", file=str(path), stdout=output)
            self.assertIn("Updated 0 of 1", output.getvalue())
        newest.refresh_from_db()
        self.assertEqual(
            newest.extensions["valuation_cost"],
            {"unit_cost": "10.000000", "source": "odoo_bill", "reference": "B1, B2"},
        )
        self.assertIn("supply_delivery_extension", newest.extensions)
        row = self.row(6)
        self.assertEqual(round(row["value"], 6), Decimal(50 * 10 + 40 * 8))
        self.assertIn("odoo_bill", row["cost_sources"])

    def test_unknown_line_saves_nothing(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "costs.csv"
            path.write_text(f"care_line_id,price_subtotal\n{uuid4()},500\n")
            with self.assertRaises(CommandError):
                call_command("import_valuation_costs", file=str(path), stdout=io.StringIO())

    def test_stock_without_layer_uses_product_price_and_says_so(self):
        # A patient return adds 100 units that no purchase layer covers.
        order = baker.make(
            DeliveryOrder,
            origin=None,
            destination=self.store,
            patient=self.encounter.patient,
            status="pending",
        )
        line = baker.make(
            SupplyDelivery,
            order=order,
            status="completed",
            supplied_item=self.batch,
            supplied_inventory_item=self.item(self.store),
            supplied_item_quantity=Decimal(100),
        )
        self.dated(SupplyDelivery, line, 5)
        row = self.row(6)
        self.assertEqual(row["quantity"], 190)
        self.assertIn("no_layer_product_price", row["cost_sources"])
        self.assertEqual(round(row["value"], 6), Decimal(50 * 15 + 100 * 8 + 40 * 10))

    def test_matches_live_stock_in_held_locations(self):
        for item in InventoryItem.objects.all():
            sync_inventory_item(inventory_item=item)
        live = InventoryItem.objects.filter(location__inventory_usage__in=["store", "none"]).aggregate(
            total=Sum("net_content")
        )["total"]
        self.assertEqual(self.row(30)["quantity"], live)

    def test_api_returns_csv_for_superusers_only(self):
        url = "/api/care_odoo/stock-valuation/"
        query = {"facility": str(self.facility.external_id), "as_of": "2026-04-06"}
        self.client.force_authenticate(self.create_user())
        self.assertEqual(self.client.get(url, query).status_code, 403)
        self.client.force_authenticate(self.create_user(is_superuser=True))
        response = self.client.get(url, {**query, "output": "csv"})
        self.assertEqual(response.status_code, 200)
        lines = response.content.decode().splitlines()
        self.assertTrue(lines[0].startswith("product_id,product_name,batch"))
        self.assertEqual(len(lines), 2)
