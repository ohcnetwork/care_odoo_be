from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

from care.emr.models.inventory_item import InventoryItem
from care.emr.models.medication_dispense import MedicationDispense
from care.emr.models.product import Product
from care.emr.models.supply_delivery import SupplyDelivery
from django.test import TestCase
from model_bakery import baker

from care_odoo.resources.stock.period import build_period_plan


class StockPeriodSourceTests(TestCase):
    def test_approved_opening_keeps_care_quantities_and_carries_named_nonstore_stock(self):
        user = baker.make("users.User")
        facility = baker.make("facility.Facility", created_by=user)
        store = baker.make("emr.FacilityLocation", facility=facility, name="Store", inventory_usage="store")
        ward = baker.make("emr.FacilityLocation", facility=facility, name="Ward", inventory_usage="none")
        baker.make("emr.FacilityLocation", facility=facility, name="Other ward", inventory_usage="none")
        supplier = baker.make("emr.Organization")
        knowledge = baker.make("emr.ProductKnowledge", facility=facility, name="Medicine")
        product = baker.make(Product, facility=facility, product_knowledge=knowledge, purchase_price=Decimal("99"))
        inventories = {}
        for location, quantity in [(store, 100), (ward, 10)]:
            inventories[location.pk] = baker.make(InventoryItem, product=product, location=location)
            order = baker.make("emr.DeliveryOrder", destination=location, supplier=supplier, origin=None, patient=None)
            receipt = baker.make(
                SupplyDelivery,
                order=order,
                supplied_inventory_item=inventories[location.pk],
                supplied_item=product,
                status="completed",
                supplied_item_quantity=quantity,
                supplied_item_pack_size=1,
                extensions={},
            )
            SupplyDelivery.objects.filter(pk=receipt.pk).update(created_date=datetime(2026, 3, 10, tzinfo=UTC))
        order = baker.make("emr.DeliveryOrder", destination=ward, origin=store, patient=None)
        transfer = baker.make(
            SupplyDelivery,
            order=order,
            supplied_inventory_item=inventories[store.pk],
            supplied_item=product,
            status="completed",
            supplied_item_quantity=20,
            extensions={},
        )
        SupplyDelivery.objects.filter(pk=transfer.pk).update(created_date=datetime(2026, 4, 10, tzinfo=UTC))
        approved = {
            "rows": [
                {"location": "Store", "name": "Medicine", "quantity": "105", "value": "800"},
                {"location": "Ward", "name": "Medicine", "quantity": "10", "value": "200"},
            ],
            "source_name": "approved.xlsx",
            "source_sha256": "fixture",
            "total": "1000.00",
            "raw_total": "1000",
        }
        with patch("care_odoo.resources.stock.period.OdooStockResource.bill_sources", return_value={"bill_lines": []}):
            plan = build_period_plan(facility, "2026-04", approved_opening=approved)
        self.assertEqual({r["name"] for r in plan["locations"]}, {"Store", "Ward"})
        self.assertEqual(sum(Decimal(r["quantity"]) for r in plan["opening"]), 110)
        self.assertEqual(sum(Decimal(r["input_value"]) for r in plan["phases"]["opening_in"]), 1000)
        self.assertEqual(Decimal(plan["items"][0]["opening_value_target"]), 1000)
        self.assertEqual(Decimal(plan["phases"]["internal"][0]["quantity"]), 20)
        self.assertEqual(plan["phases"]["issues"], [])
        self.assertEqual(plan["metadata"]["approved_opening"]["total"], "1000.00")

    def test_zero_balance_batches_do_not_break_the_plan_and_real_flows_drive_closing(self):
        user = baker.make("users.User", username="stock-source-test")
        facility = baker.make("facility.Facility", created_by=user)
        store = baker.make("emr.FacilityLocation", facility=facility, inventory_usage="store")
        supplier = baker.make("emr.Organization")
        patient = baker.make("emr.Patient")
        encounter = baker.make("emr.Encounter", patient=patient, facility=facility)
        order = baker.make("emr.DeliveryOrder", destination=store, supplier=supplier, origin=None, patient=None)
        for quantity, dispensed in [(10, 10), (30, 12)]:
            knowledge = baker.make("emr.ProductKnowledge", facility=facility)
            product = baker.make(Product, facility=facility, product_knowledge=knowledge, purchase_price=Decimal("2"))
            inventory = baker.make(InventoryItem, product=product, location=store, net_content=quantity - dispensed)
            receipt = baker.make(
                SupplyDelivery,
                order=order,
                supplied_item=product,
                supplied_inventory_item=inventory,
                status="completed",
                supplied_item_quantity=quantity,
                supplied_item_pack_size=1,
                supplied_item_pack_quantity=quantity,
                extensions={},
            )
            SupplyDelivery.objects.filter(pk=receipt.pk).update(created_date=datetime(2026, 3, 10, tzinfo=UTC))
            issue = baker.make(
                MedicationDispense,
                item=inventory,
                location=store,
                quantity=dispensed,
                status="completed",
                patient=patient,
                encounter=encounter,
            )
            MedicationDispense.objects.filter(pk=issue.pk).update(
                created_date=datetime(2026, 3 if quantity == 10 else 4, 20, tzinfo=UTC)
            )
        with patch("care_odoo.resources.stock.period.OdooStockResource.bill_sources", return_value={"bill_lines": []}):
            plan = build_period_plan(facility, "2026-04")
        self.assertEqual(len(plan["items"]), 1)
        self.assertEqual(Decimal(plan["opening"][0]["quantity"]), 30)
        self.assertEqual(Decimal(plan["closing"][0]["quantity"]), 18)
        self.assertEqual(Decimal(plan["phases"]["issues"][0]["quantity"]), 12)
        self.assertEqual(plan["phases"]["receipts"], [])
