import json
from pathlib import Path

from care.facility.models.facility import Facility
from django.core.management.base import BaseCommand, CommandError

from care_odoo.resources.stock.export import export_month
from care_odoo.resources.stock.service import freeze_month, post_month, rebuild_months


class Command(BaseCommand):
    help = "Prepare/post monthly stock summaries, or rebuild and archive a previous series"

    def add_arguments(self, parser):
        parser.add_argument("--facility", required=True)
        parser.add_argument("--month", required=True)
        parser.add_argument("--fiscal-month", required=True)
        parser.add_argument("--expected-opening-total")
        parser.add_argument("--post", action="store_true")
        parser.add_argument(
            "--rebuild", action="store_true", help="Rebuild from the saved opening through the latest processed month"
        )
        parser.add_argument("--output", required=True)

    def handle(self, *args, **options):
        folder = Path(options["output"])
        folder.mkdir(parents=True, exist_ok=True)
        try:
            facility = Facility.objects.get(external_id=options["facility"])
            if options["rebuild"]:
                if not options["post"]:
                    raise ValueError("Use --post with --rebuild to replace the reports in both systems")
                rows = rebuild_months(
                    facility,
                    options["month"],
                    options["fiscal_month"],
                    options["expected_opening_total"],
                    self.stdout.write,
                )
                for row in rows:
                    export_month(row, folder / str(row.revision) / row.month)
                return
            row = freeze_month(
                facility,
                options["month"],
                options["fiscal_month"],
                options["expected_opening_total"],
                self.stdout.write,
            )
            (folder / "plan.json").write_text(json.dumps(row.plan, indent=2))
            if row.plan.get("opening_source"):
                (folder / "opening-source.json").write_text(json.dumps(row.plan["opening_source"], indent=2))
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
