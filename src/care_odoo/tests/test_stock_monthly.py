import csv
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from care.emr.models.medication_dispense import MedicationDispense
from care.emr.models.supply_delivery import SupplyDelivery
from care.emr.signals.patient.base import BasePatientIdentifierConfig
from django.db import DatabaseError, transaction
from django.test import TestCase
from model_bakery import baker

from care_odoo.models import StockMonth
from care_odoo.resources.stock.export import export_month
from care_odoo.resources.stock.monthly import build_monthly_plan, fingerprint
from care_odoo.resources.stock.service import freeze_month, post_month, target


class MonthlyStockTests(TestCase):
    def tearDown(self):
        for config in BasePatientIdentifierConfig.__subclasses__():
            config.CACHED_CONFIG.clear()
        super().tearDown()

    def test_opening_is_captured_from_care_and_frozen_against_later_price_changes(self):
        user = baker.make("users.User")
        facility = baker.make("facility.Facility", created_by=user)
        store = baker.make("emr.FacilityLocation", facility=facility, name="Store", inventory_usage="store")
        ward = baker.make("emr.FacilityLocation", facility=facility, name="Ward", inventory_usage="none")
        consumption = baker.make("emr.FacilityLocation", facility=facility, inventory_usage="consumption")
        item = baker.make("emr.ProductKnowledge", facility=facility, name="Medicine")
        product = baker.make("emr.Product", facility=facility, product_knowledge=item, purchase_price=8)
        inventory = baker.make("emr.InventoryItem", product=product, location=store)
        consumed = baker.make("emr.InventoryItem", product=product, location=consumption)
        for origin, destination, source, status, quantity, created in [
            (None, store, inventory, "completed", 100, datetime(2026, 3, 10, tzinfo=UTC)),
            (store, ward, inventory, "in_progress", 20, datetime(2026, 3, 15, tzinfo=UTC)),
            (consumption, ward, consumed, "in_progress", 3, datetime(2026, 3, 20, tzinfo=UTC)),
            (None, store, inventory, "completed", 50, datetime(2026, 4, 10, tzinfo=UTC)),
        ]:
            order = baker.make("emr.DeliveryOrder", origin=origin, destination=destination, patient=None)
            delivery = baker.make(
                SupplyDelivery,
                order=order,
                supplied_item=product,
                supplied_inventory_item=source,
                supplied_item_quantity=quantity,
                supplied_item_pack_size=1,
                status=status,
                extensions={},
            )
            SupplyDelivery.objects.filter(pk=delivery.pk).update(created_date=created)
        patient = baker.make("emr.Patient")
        encounter = baker.make("emr.Encounter", patient=patient, facility=facility)
        dispense_order = baker.make("emr.DispenseOrder", patient=patient, facility=facility, location=store)
        for quantity, month in [(10, 3), (20, 4)]:
            dispense = baker.make(
                MedicationDispense,
                item=inventory,
                location=store,
                patient=patient,
                encounter=encounter,
                order=dispense_order,
                quantity=quantity,
                status="completed",
            )
            MedicationDispense.objects.filter(pk=dispense.pk).update(created_date=datetime(2026, month, 21, tzinfo=UTC))
        # The historical report values 93 priced units at 8. Correct owned
        # quantities exclude the three pending returns from consumption.
        with self.assertRaisesRegex(ValueError, "differs from the approved total"):
            freeze_month(facility, "2026-04", "2026-04", expected_opening_total="745")
        self.assertFalse(StockMonth.objects.filter(facility=facility).exists())
        with patch("care_odoo.resources.stock.period.OdooStockResource.bill_sources", return_value={"bill_lines": []}):
            row = freeze_month(facility, "2026-04", "2026-04", expected_opening_total="744")
        uuid = str(item.external_id)
        self.assertEqual(Decimal(row.plan["opening_quantities"][uuid]), 90)
        self.assertEqual(Decimal(row.plan["closing_quantities"][uuid]), 120)
        self.assertEqual(Decimal(row.plan["items"][0]["opening_value_target"]), 744)
        self.assertEqual(row.plan["opening_source"]["as_of"], "2026-03-31")
        self.assertEqual(row.plan["opening_source"]["total"], "744.00")
        self.assertEqual(sum(Decimal(r["value"]) for r in row.plan["opening_source"]["rows"]), 744)
        frozen_plan, frozen_hash = row.plan, row.source_hash
        product.purchase_price = 99
        product.save(update_fields=["purchase_price"])
        retry = freeze_month(facility, "2026-04", "2026-04", expected_opening_total="744")
        self.assertEqual(retry.plan, frozen_plan)
        self.assertEqual(retry.source_hash, frozen_hash)
        with self.assertRaisesRegex(ValueError, "differs from the frozen source"):
            freeze_month(facility, "2026-04", "2026-04", expected_opening_total="9207")

    def test_owned_stock_keeps_pending_internal_transfers_and_new_stock_locations(self):
        user = baker.make("users.User")
        facility = baker.make("facility.Facility", created_by=user)
        store = baker.make("emr.FacilityLocation", facility=facility, name="Store", inventory_usage="store")
        ward = baker.make("emr.FacilityLocation", facility=facility, name="New ward", inventory_usage="none")
        knowledge = baker.make("emr.ProductKnowledge", facility=facility, name="Medicine")
        product = baker.make("emr.Product", facility=facility, product_knowledge=knowledge, purchase_price=None)
        inventory = baker.make("emr.InventoryItem", product=product, location=store)
        order = baker.make("emr.DeliveryOrder", destination=store, origin=None, patient=None)
        receipt = baker.make(
            SupplyDelivery,
            order=order,
            supplied_inventory_item=inventory,
            supplied_item=product,
            status="completed",
            supplied_item_quantity=100,
            extensions={},
        )
        SupplyDelivery.objects.filter(pk=receipt.pk).update(created_date=datetime(2026, 3, 10, tzinfo=UTC))
        order = baker.make("emr.DeliveryOrder", destination=ward, origin=store, patient=None, status="draft")
        transfer = baker.make(
            SupplyDelivery,
            order=order,
            supplied_inventory_item=inventory,
            supplied_item=product,
            status="in_progress",
            supplied_item_quantity=20,
            extensions={},
        )
        SupplyDelivery.objects.filter(pk=transfer.pk).update(created_date=datetime(2026, 4, 10, tzinfo=UTC))
        consumption = baker.make("emr.FacilityLocation", facility=facility, inventory_usage="consumption")
        consumed = baker.make("emr.InventoryItem", product=product, location=consumption)
        return_order = baker.make("emr.DeliveryOrder", destination=ward, origin=consumption, patient=None)
        returned = baker.make(
            SupplyDelivery,
            order=return_order,
            supplied_inventory_item=consumed,
            supplied_item=product,
            status="in_progress",
            supplied_item_quantity=5,
            extensions={},
        )
        SupplyDelivery.objects.filter(pk=returned.pk).update(created_date=datetime(2026, 4, 10, tzinfo=UTC))
        approved = {
            "rows": [{"location": "Store", "name": "Medicine", "quantity": "100", "value": "800"}],
            "source_name": "opening.xlsx",
            "source_sha256": "fixture",
            "total": "800.00",
            "raw_total": "800",
        }
        plan = build_monthly_plan(facility, "2026-04", "2026-04", approved_opening=approved)
        uuid = str(knowledge.external_id)
        self.assertEqual(Decimal(plan["closing_quantities"][uuid]), 100)
        self.assertEqual(Decimal(plan["available_quantities"][uuid]), 80)
        self.assertEqual(Decimal(plan["in_transit_quantities"][uuid]), 20)
        self.assertEqual(plan["phases"]["issues"], [])
        self.assertEqual(Decimal(plan["items"][0]["opening_value_target"]), 800)
        incoming = plan["evidence"]["in_transit_closing"]
        self.assertEqual(incoming[0]["location_uuid"], str(ward.external_id))
        self.assertEqual(Decimal(incoming[0]["quantity"]), 20)
        SupplyDelivery.objects.filter(pk=returned.pk).update(status="completed")
        completed = build_monthly_plan(facility, "2026-04", "2026-04", approved_opening=approved)
        self.assertEqual(Decimal(completed["closing_quantities"][uuid]), 105)
        self.assertEqual(Decimal(completed["available_quantities"][uuid]), 85)
        self.assertEqual(Decimal(completed["in_transit_quantities"][uuid]), 20)
        self.assertEqual(Decimal(completed["phases"]["recoveries"][0]["quantity"]), 5)

    def test_remote_close_recovers_after_the_local_commit_or_response_is_lost(self):
        user = baker.make("users.User")
        facility = baker.make("facility.Facility", created_by=user)
        plan = {
            "period": {
                "period_key": f"recover:{facility.external_id}",
                "month": "2026-04",
                "facility_uuid": str(facility.external_id),
                "mode": "monthly",
            },
            "items": [{"uuid": str(facility.external_id), "name": "Recovery fixture"}],
            "opening_at": "2026-03-31T18:29:59Z",
        }
        row = StockMonth.objects.create(
            facility=facility,
            month="2026-04",
            fiscal_month="2026-04",
            target=target(),
            plan=plan,
            source_hash=fingerprint(plan),
        )
        closed = {"month": "2026-04", "source_hash": row.source_hash, "items": [], "total_value": "0.00"}
        with (
            patch("care_odoo.resources.stock.service.plugin_settings.CARE_ODOO_STOCK_SYNC_ENABLED", True),
            patch(
                "care_odoo.resources.stock.service.OdooStockResource.call",
                return_value={"closed": True, "snapshot": closed},
            ),
            patch(
                "care_odoo.resources.stock.service.OdooStockResource.prepare",
                side_effect=AssertionError("Remote month is already closed"),
            ),
        ):
            self.assertEqual(post_month(row.pk), closed)
        row.refresh_from_db()
        self.assertEqual(row.state, "closed")
        self.assertEqual(row.closing, closed)

    def test_actual_flows_free_units_rollforward_and_frozen_source_guards(self):
        user = baker.make("users.User")
        facility = baker.make("facility.Facility", created_by=user)
        store = baker.make("emr.FacilityLocation", facility=facility, name="Store", inventory_usage="store")
        supplier = baker.make("emr.Organization")
        knowledge = baker.make("emr.ProductKnowledge", facility=facility, name="Medicine")
        product = baker.make("emr.Product", facility=facility, product_knowledge=knowledge, purchase_price=10)
        inventory = baker.make("emr.InventoryItem", product=product, location=store)
        for quantity, created, extensions in [
            (100, datetime(2026, 3, 10, tzinfo=UTC), {}),
            (
                150,
                datetime(2026, 4, 10, tzinfo=UTC),
                {"supply_delivery_extension": {"free_quantity": 50, "purchase_discount": 10}},
            ),
        ]:
            order = baker.make("emr.DeliveryOrder", destination=store, supplier=supplier, origin=None, patient=None)
            receipt = baker.make(
                SupplyDelivery,
                order=order,
                supplied_inventory_item=inventory,
                supplied_item=product,
                status="completed",
                supplied_item_quantity=quantity,
                supplied_item_pack_size=1,
                extensions=extensions,
            )
            SupplyDelivery.objects.filter(pk=receipt.pk).update(created_date=created)
        patient = baker.make("emr.Patient")
        encounter = baker.make("emr.Encounter", patient=patient, facility=facility)
        dispense = baker.make(
            MedicationDispense,
            item=inventory,
            location=store,
            quantity=170,
            status="completed",
            patient=patient,
            encounter=encounter,
        )
        MedicationDispense.objects.filter(pk=dispense.pk).update(created_date=datetime(2026, 4, 20, tzinfo=UTC))
        approved = {
            "rows": [{"location": "Store", "name": "Medicine", "quantity": "100", "value": "800"}],
            "source_name": "opening.xlsx",
            "source_sha256": "fixture",
            "total": "800.00",
            "raw_total": "800",
        }
        with patch("care_odoo.resources.stock.period.OdooStockResource.bill_sources", return_value={"bill_lines": []}):
            april = build_monthly_plan(facility, "2026-04", "2026-04", approved_opening=approved)
        uuid = str(knowledge.external_id)
        self.assertEqual(Decimal(april["closing_quantities"][uuid]), 80)
        self.assertEqual(Decimal(april["phases"]["receipts"][0]["quantity"]), 150)
        self.assertEqual(Decimal(april["phases"]["receipts"][0]["input_value"]), 900)
        with (
            patch(
                "care_odoo.resources.stock.period.plugin_settings.CARE_ODOO_INTERNAL_SUPPLIER_ID",
                str(supplier.external_id),
            ),
            patch("care_odoo.resources.stock.period.OdooStockResource.bill_sources", return_value={"bill_lines": []}),
        ):
            internal = build_monthly_plan(facility, "2026-04", "2026-04", approved_opening=approved)
        self.assertEqual(internal["phases"]["receipts"][0]["receipts"][0]["receipt_type"], "stock_adjustment")
        self.assertEqual(Decimal(internal["phases"]["receipts"][0]["input_value"]), 900)
        self.assertEqual(internal["metadata"]["flag_counts"]["internal_stock_adjustment_care_cost"], 1)
        previous = {
            "month": "2026-04",
            "quantity_basis": "owned",
            "total_value": "544.00",
            "location_uuids": [str(store.external_id)],
            "items": [
                {
                    "item_uuid": uuid,
                    "name": "Medicine",
                    "quantity": 80,
                    "average_cost": "6.8",
                    "reported_total_value": 544,
                }
            ],
        }
        may = build_monthly_plan(facility, "2026-05", "2026-04", previous_close=previous)
        self.assertEqual(may["phases"]["opening_in"], [])
        self.assertEqual(may["phases"]["opening_out"], [])
        self.assertEqual(Decimal(may["items"][0]["opening_unit_cost"]), Decimal("6.8"))
        self.assertEqual(Decimal(may["items"][0]["opening_value_target"]), 544)
        self.assertEqual(Decimal(may["closing_quantities"][uuid]), 80)
        # A backdated source edit cannot quietly change the previous approved close.
        MedicationDispense.objects.filter(pk=dispense.pk).update(quantity=171)
        with self.assertRaisesRegex(ValueError, "opening changed"):
            build_monthly_plan(facility, "2026-05", "2026-04", previous_close=previous)
        row = StockMonth.objects.create(
            facility=facility,
            month="2026-04",
            fiscal_month="2026-04",
            target=target(),
            plan=april,
            source_hash=fingerprint(april),
        )
        delivered = []

        def record_events(_period, events):
            delivered.extend(event["kind"] for event in events)
            return {"posted": len(events)}

        with (
            patch("care_odoo.resources.stock.service.plugin_settings.CARE_ODOO_STOCK_SYNC_ENABLED", True),
            patch("care_odoo.resources.stock.service.OdooStockResource.prepare", return_value={}),
            patch("care_odoo.resources.stock.service.OdooStockResource.post_events", side_effect=record_events),
            patch(
                "care_odoo.resources.stock.service.OdooStockResource.report",
                return_value={"items": [{"item_uuid": uuid, "reported_total_value": 800}]},
            ),
            patch("care_odoo.resources.stock.service.OdooStockResource.call", return_value={"snapshot": previous}),
        ):
            post_month(row.pk)
        self.assertEqual(delivered, ["opening", "receipt", "dispense"])
        row.refresh_from_db()
        with TemporaryDirectory() as folder:
            export_month(row, folder)
            with (Path(folder) / "stock-by-batch-location.csv").open() as stream:
                exported = list(csv.DictReader(stream))
            self.assertEqual(sum(Decimal(r["quantity"]) for r in exported), 80)
            self.assertEqual(sum(Decimal(r["value"]) for r in exported), 544)
            self.assertIn("receipt_without_posted_bill", exported[0]["flags"])
        with self.assertRaises(DatabaseError), transaction.atomic():
            StockMonth.objects.filter(pk=row.pk).update(plan={})
        for operation in [
            lambda: StockMonth.objects.filter(pk=row.pk).update(closing={}),
            lambda: StockMonth.objects.filter(pk=row.pk).delete(),
        ]:
            with self.assertRaises(DatabaseError), transaction.atomic():
                operation()
