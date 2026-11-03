import json
from collections import Counter

from care.emr.models.supply_delivery import SupplyDelivery
from care.facility.models import Facility
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from care_odoo.connector.connector import OdooConnector
from care_odoo.resources.account_move.purchase import purchase_metadata


class Command(BaseCommand):
    help = "Backfill Care item/batch/unit identities on existing Odoo purchase lines; financial fields are untouched."

    def add_arguments(self, parser):
        parser.add_argument("--facility", required=True, help="Care facility UUID")
        parser.add_argument("--post", action="store_true", help="Write metadata to Odoo; default is a local dry run")

    def handle(self, *args, **options):
        try:
            facility = Facility.objects.get(external_id=options["facility"])
        except (Facility.DoesNotExist, ValueError, ValidationError) as error:
            raise CommandError("Facility not found.") from error
        receipts = (
            SupplyDelivery.objects.filter(
                supplied_item__facility=facility,
                order__origin__isnull=True,
                order__patient__isnull=True,
            )
            .select_related(
                "supplied_item__facility",
                "supplied_item__product_knowledge",
                "supplied_item__charge_item_definition",
            )
            .order_by("pk")
        )
        counts = Counter()
        pending = []

        def send():
            if options["post"]:
                result = OdooConnector.call_api("api/purchases/metadata", {"entries": pending})
                counts.update(row["status"] for row in result["results"])
            pending.clear()

        for receipt in receipts.iterator(chunk_size=500):
            metadata = purchase_metadata(receipt)
            if metadata is None:
                counts["missing_identity_or_unit"] += 1
                continue
            counts["prepared"] += 1
            pending.append(
                {"care_line_uuid": str(receipt.external_id), "care_purchase": metadata.model_dump(mode="json")}
            )
            if len(pending) == 500:
                send()
        if pending:
            send()
        self.stdout.write(json.dumps({"posted": options["post"], **counts}, sort_keys=True))
