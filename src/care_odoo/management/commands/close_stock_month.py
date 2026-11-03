import json
from pathlib import Path

from care.facility.models.facility import Facility
from django.core.management.base import BaseCommand, CommandError

from care_odoo.resources.stock.export import export_month
from care_odoo.resources.stock.opening import load_opening_report
from care_odoo.resources.stock.service import freeze_month, post_month


class Command(BaseCommand):
    help = "Freeze a Care inventory month, optionally post product summaries and close in Odoo"

    def add_arguments(self, parser):
        parser.add_argument("--facility", required=True)
        parser.add_argument("--month", required=True)
        parser.add_argument("--fiscal-month", required=True)
        parser.add_argument("--opening-report")
        parser.add_argument("--expected-opening-total")
        parser.add_argument("--post", action="store_true")
        parser.add_argument("--output", required=True)

    def handle(self, *args, **options):
        folder = Path(options["output"])
        folder.mkdir(parents=True, exist_ok=True)
        try:
            approved = (
                load_opening_report(options["opening_report"], options["expected_opening_total"])
                if options["opening_report"]
                else None
            )
            row = freeze_month(
                Facility.objects.get(external_id=options["facility"]),
                options["month"],
                options["fiscal_month"],
                approved,
                self.stdout.write,
            )
            (folder / "plan.json").write_text(json.dumps(row.plan, indent=2))
            self.stdout.write(
                json.dumps(
                    {
                        "month": row.month,
                        "source_hash": row.source_hash,
                        "summary_counts": row.plan["metadata"]["summary_event_counts"],
                        "flags": row.plan["metadata"]["flag_counts"],
                    }
                )
            )
            if options["post"]:
                result = post_month(row.pk, self.stdout.write)
                row.refresh_from_db()
                (folder / "posting-progress.json").write_text(json.dumps(row.progress, indent=2))
                export_month(row, folder)
                self.stdout.write(f"Closed {row.month}: {result['total_value']}")
        except ValueError as error:
            raise CommandError(str(error)) from error
