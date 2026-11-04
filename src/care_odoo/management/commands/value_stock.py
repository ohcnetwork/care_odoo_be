from decimal import Decimal
from pathlib import Path

from care.facility.models.facility import Facility
from django.core.management.base import BaseCommand, CommandError

from care_odoo.resources.stock_valuation.valuation import value_inventory
from care_odoo.resources.stock_valuation.views import end_of_day, write_csv


class Command(BaseCommand):
    help = "Write a FIFO stock valuation for a facility at the end of a day (India time)"

    def add_arguments(self, parser):
        parser.add_argument("--facility", required=True)
        parser.add_argument("--as-of", required=True, help="YYYY-MM-DD")
        parser.add_argument("--output", required=True)

    def handle(self, *args, **options):
        target = Path(options["output"])
        if target.exists():
            raise CommandError("Output file already exists")
        facility = Facility.objects.get(external_id=options["facility"])
        rows = value_inventory(facility, end_of_day(options["as_of"]))
        with target.open("x", newline="") as handle:
            write_csv(rows, handle)
        total = sum((row["value"] or Decimal(0) for row in rows), Decimal(0))
        flagged = sum(1 for row in rows if row["flags"])
        self.stdout.write(f"{len(rows):,} rows, value {total:,.2f}, {flagged:,} flagged, written to {target}")
