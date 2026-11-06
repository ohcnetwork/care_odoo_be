import csv
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from care_odoo.resources.stock_valuation.bill_costs import ODOO_BILL, cost_changes


class Command(BaseCommand):
    help = (
        "Write confirmed purchase costs (e.g. posted Odoo bill lines) onto Care receipt lines. "
        "CSV columns: care_line_id and price_subtotal (line total excluding tax); optional reference. "
        "Several rows for one Care line are added together. Safe to rerun. "
        "sync_valuation_costs reads posted bills from Odoo directly."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)
        parser.add_argument("--source", default=ODOO_BILL)

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
        changes = cost_changes(subtotals, references, options["source"])
        problems += changes.problems
        if problems:
            raise CommandError(f"Nothing saved; {len(problems)} problems:\n" + "\n".join(problems[:20]))
        changes.save()
        self.stdout.write(f"Updated {changes.changed:,} of {changes.lines:,} receipt lines from {path}")
