from django.core.management.base import BaseCommand, CommandError
from rest_framework.exceptions import ValidationError

from care_odoo.resources.stock_valuation.bill_costs import bill_cost_changes


class Command(BaseCommand):
    help = (
        "Store the cost of every posted Odoo vendor bill line on its Care receipt line for stock valuation, "
        "and take off costs whose bill is no longer posted. Reads Odoo directly. Safe to rerun."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would change without saving")

    def handle(self, *args, **options):
        try:
            changes = bill_cost_changes()
        except ValidationError as error:  # the connector's error when Odoo can't be reached or refuses
            raise CommandError(f"Nothing saved: {' '.join(error.detail)}") from error
        except ValueError as error:
            raise CommandError(f"Nothing saved: {error}") from error
        if not options["dry_run"]:
            changes.save()
        self.stdout.write(
            ("Dry run, nothing saved. " if options["dry_run"] else "")
            + f"{changes.lines:,} receipt lines on posted Odoo bills: {changes.changed:,} costs set or changed, "
            f"{changes.removed:,} taken off as their bill is no longer posted"
        )
        if changes.problems:
            self.stdout.write(f"Skipped {len(changes.problems):,}:")
            for problem in changes.problems[:20]:
                self.stdout.write(f"  {problem}")
        if changes.quantity_differences:
            self.stdout.write(
                f"{len(changes.quantity_differences):,} bills received another quantity than Care sent "
                "(receipt line: Care, bill):"
            )
            for line_id, sent, received in changes.quantity_differences[:20]:
                self.stdout.write(f"  {line_id}: {float(sent):g}, {float(received):g}")
