from decimal import Decimal
from pathlib import Path

from care.facility.models.facility import Facility
from django.core.management.base import BaseCommand, CommandError

from care_odoo.resources.stock_valuation.valuation import end_of_day, value_inventory, write_csv


class Command(BaseCommand):
    help = "Write a FIFO stock valuation for a facility, as of the end of a day, to a CSV file"

    def add_arguments(self, parser):
        parser.add_argument("--facility", required=True, help="Facility external id")
        parser.add_argument("--as-of", required=True, help="YYYY-MM-DD")
        parser.add_argument("--output", required=True, help="New CSV file; an existing file is not overwritten")

    def handle(self, *args, **options):
        target = Path(options["output"])
        if target.exists():
            raise CommandError("Output file already exists")
        try:
            facility = Facility.objects.get(external_id=options["facility"])
            as_of = end_of_day(options["as_of"])
        except (ValueError, Facility.DoesNotExist) as error:
            raise CommandError("--facility must be a facility id and --as-of a YYYY-MM-DD date") from error
        rows = value_inventory(facility, as_of)
        with target.open("x", newline="") as handle:
            write_csv(rows, handle)
        total = sum((row["value"] or Decimal(0) for row in rows), Decimal(0))
        flagged = sum(1 for row in rows if row["flags"])
        batches = len({row["product_id"] for row in rows})
        self.stdout.write(
            f"{batches:,} batches in {len(rows):,} rows by location, value {total:,.2f}, {flagged:,} flagged, "
            f"written to {target}"
        )
