import csv
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from care.emr.models.supply_delivery import SupplyDelivery
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from care_odoo.extensions import VALUATION_COST


class Command(BaseCommand):
    help = (
        "Write confirmed purchase costs (e.g. posted Odoo bill lines) onto Care receipt lines. "
        "CSV columns: care_line_id and price_subtotal (line total excluding tax); optional reference. "
        "Several rows for one Care line are added together. Safe to rerun."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)
        parser.add_argument("--source", default="odoo_bill")

    @transaction.atomic
    def handle(self, *args, **options):
        path = Path(options["file"])
        subtotals, references, problems = defaultdict(Decimal), defaultdict(list), []
        with path.open(newline="") as handle:
            for number, row in enumerate(csv.DictReader(handle), start=2):
                try:
                    subtotals[row["care_line_id"]] += Decimal(row["price_subtotal"])
                except (KeyError, TypeError, InvalidOperation):
                    problems.append(f"row {number}: needs care_line_id and a numeric price_subtotal")
                    continue
                if row.get("reference"):
                    references[row["care_line_id"]].append(row["reference"])
        try:
            lines = SupplyDelivery.objects.in_bulk(list(subtotals), field_name="external_id")
        except ValidationError as error:
            raise CommandError("Every care_line_id must be a UUID") from error
        lines = {str(key): line for key, line in lines.items()}
        changed = []
        for line_id, subtotal in subtotals.items():
            line = lines.get(line_id)
            if line is None or not line.supplied_item_quantity:
                problems.append(f"{line_id}: no Care receipt line with a quantity")
                continue
            cost = {
                "unit_cost": str((subtotal / line.supplied_item_quantity).quantize(Decimal("0.000001"))),
                "source": options["source"],
                "reference": ", ".join(references[line_id]),
            }
            if (line.extensions or {}).get(VALUATION_COST) != cost:
                line.extensions = {**(line.extensions or {}), VALUATION_COST: cost}
                changed.append(line)
        if problems:
            raise CommandError(f"Nothing saved; {len(problems)} problems:\n" + "\n".join(problems[:20]))
        # bulk_update skips save hooks, so there is no Odoo resync or modified_date change.
        SupplyDelivery.objects.bulk_update(changed, ["extensions"], batch_size=2000)
        self.stdout.write(f"Updated {len(changed):,} of {len(subtotals):,} receipt lines from {path}")
