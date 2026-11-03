from django.core.management.base import BaseCommand, CommandError

from care_odoo.models import StockMonth
from care_odoo.resources.stock.export import export_month


class Command(BaseCommand):
    help = "Export an issued inventory snapshot without fetching stock/prices or writing to Odoo"

    def add_arguments(self, parser):
        parser.add_argument("--facility", required=True)
        parser.add_argument("--month", required=True)
        parser.add_argument("--revision", help="Export a saved previous revision instead of the current report")
        parser.add_argument("--output", required=True)

    def handle(self, *args, **options):
        try:
            reports = StockMonth.objects.filter(
                facility__external_id=options["facility"], month=options["month"], state="closed"
            )
            row = reports.get(revision=options["revision"]) if options["revision"] else reports.get(is_current=True)
        except StockMonth.DoesNotExist as error:
            raise CommandError("No issued inventory snapshot exists for this facility/month") from error
        export_month(row, options["output"])
        self.stdout.write(f"Exported {row.month}: {row.closing['total_value']}")
