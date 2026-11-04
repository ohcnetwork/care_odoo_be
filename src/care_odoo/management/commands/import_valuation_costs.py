import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from care.emr.models.supply_delivery import SupplyDelivery
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from care_odoo.extensions import VALUATION_COST


class Command(BaseCommand):
    help = (
        "Write confirmed purchase costs (e.g. posted Odoo bill lines) onto Care "
        "receipt lines. CSV columns: care_line_id and either unit_cost (per base "
        "unit) or price_subtotal (line total excluding tax, spread over the Care "
        "line's base units); reference is optional. Safe to rerun."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)
        parser.add_argument("--source", default="odoo_bill")

    @transaction.atomic
    def handle(self, *args, **options):
        path = Path(options["file"])
        with path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        lines = {
            str(key): line
            for key, line in SupplyDelivery.objects.in_bulk(
                [row["care_line_id"] for row in rows], field_name="external_id"
            ).items()
        }
        changed, problems = [], []
        for number, row in enumerate(rows, start=2):
            line = lines.get(row["care_line_id"])
            try:
                if line is None:
                    raise ValueError("unknown care_line_id")
                if row.get("unit_cost"):
                    unit_cost = Decimal(row["unit_cost"])
                elif line.supplied_item_quantity:
                    unit_cost = Decimal(row["price_subtotal"]) / line.supplied_item_quantity
                else:
                    raise ValueError("line has no quantity")
            except (KeyError, ValueError, InvalidOperation) as error:
                problems.append(f"row {number}: {error}")
                continue
            cost = {
                "unit_cost": str(unit_cost.quantize(Decimal("0.000001"))),
                "source": options["source"],
                "reference": row.get("reference", ""),
            }
            if (line.extensions or {}).get(VALUATION_COST) != cost:
                line.extensions = {**(line.extensions or {}), VALUATION_COST: cost}
                changed.append(line)
        if problems:
            raise CommandError(f"Nothing saved; {len(problems)} problems:\n" + "\n".join(problems[:20]))
        # bulk_update skips save hooks, so no Odoo resync or modified_date change.
        SupplyDelivery.objects.bulk_update(changed, ["extensions"], batch_size=2000)
        self.stdout.write(f"Updated {len(changed):,} of {len(rows):,} receipt lines from {path}")
