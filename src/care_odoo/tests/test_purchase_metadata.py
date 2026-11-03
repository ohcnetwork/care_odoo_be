import json
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from care.emr.models.supply_delivery import SupplyDelivery
from django.core.management import call_command
from django.test import TestCase
from model_bakery import baker

from care_odoo.resources.account_move.delivery_order import OdooDeliveryOrderResource


class PurchaseMetadataTests(TestCase):
    def test_completed_receipts_send_batch_identity_and_backfill_uses_the_same_units(self):
        user = baker.make("users.User")
        facility = baker.make("facility.Facility", created_by=user)
        store = baker.make("emr.FacilityLocation", facility=facility, inventory_usage="store")
        supplier = baker.make("emr.Organization", metadata={})
        order = baker.make(
            "emr.DeliveryOrder", destination=store, supplier=supplier, origin=None, patient=None, extensions={}
        )
        knowledge = baker.make(
            "emr.ProductKnowledge", facility=facility, name="Medicine", base_unit={"code": "{tbl}", "display": "tablet"}
        )
        receipts = []
        for batch, packs, pack_size, quantity in [("0007", 150, 10, 1500), ("0099", None, 20, 1000)]:
            charge = baker.make(
                "emr.ChargeItemDefinition", facility=facility, status="active", category=None, price_components=[]
            )
            product = baker.make(
                "emr.Product",
                facility=facility,
                product_knowledge=knowledge,
                charge_item_definition=charge,
                standard_pack_size=pack_size,
                batch={"lot_number": batch},
            )
            receipts.append(
                baker.make(
                    SupplyDelivery,
                    order=order,
                    supplied_item=product,
                    status="completed",
                    supplied_item_pack_quantity=packs,
                    supplied_item_pack_size=pack_size,
                    supplied_item_quantity=quantity,
                    total_purchase_price=100,
                    extensions={},
                )
            )
        # This receipt has packs but no historical pack-size evidence: never guess from the current product.
        baker.make(
            SupplyDelivery,
            order=order,
            supplied_item=receipts[0].supplied_item,
            status="draft",
            supplied_item_pack_quantity=10,
            supplied_item_pack_size=None,
            supplied_item_quantity=100,
            extensions={},
        )
        with patch(
            "care_odoo.resources.account_move.delivery_order.OdooConnector.call_api",
            return_value={"invoice": {"id": 7}},
        ) as api:
            self.assertEqual(OdooDeliveryOrderResource().sync_delivery_order_to_odoo_api(str(order.external_id)), 7)
        payload = api.call_args.args[1]
        sent = {row["x_care_id"]: row for row in payload["invoice_items"]}
        first, second = (sent[str(r.external_id)] for r in receipts)
        metadata = first["care_purchase"]
        self.assertEqual(metadata["product_id"], receipts[0].supplied_item_id)
        self.assertEqual(metadata["product_uuid"], str(receipts[0].supplied_item.external_id))
        self.assertEqual(metadata["item_uuid"], str(knowledge.external_id))
        self.assertEqual(metadata["facility_uuid"], str(facility.external_id))
        self.assertEqual(metadata["batch"], "0007")
        self.assertEqual((first["quantity"], metadata["pack_size"]), ("150", 10))
        self.assertEqual((Decimal(second["quantity"]), second["care_purchase"]["pack_size"]), (Decimal("1000"), 1))
        stdout = StringIO()
        with patch("care_odoo.management.commands.sync_odoo_purchase_metadata.OdooConnector.call_api") as api:
            call_command("sync_odoo_purchase_metadata", facility=str(facility.external_id), stdout=stdout)
            api.assert_not_called()
        self.assertEqual(json.loads(stdout.getvalue()), {"posted": False, "prepared": 2, "missing_identity_or_unit": 1})
        result = {"results": [{"status": "updated"}, {"status": "unchanged"}]}
        sent_entries = []

        def capture_request(endpoint, data):
            sent_entries.extend(json.loads(json.dumps(data))["entries"])
            return result

        with patch(
            "care_odoo.management.commands.sync_odoo_purchase_metadata.OdooConnector.call_api",
            side_effect=capture_request,
        ):
            call_command(
                "sync_odoo_purchase_metadata", facility=str(facility.external_id), post=True, stdout=StringIO()
            )
        entries = {row["care_line_uuid"]: row["care_purchase"] for row in sent_entries}
        self.assertEqual(entries[str(receipts[0].external_id)], first["care_purchase"])
        self.assertEqual(entries[str(receipts[1].external_id)], second["care_purchase"])
