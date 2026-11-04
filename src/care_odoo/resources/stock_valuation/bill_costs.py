"""
Confirmed purchase costs on Care receipt lines, for stock valuation.

Every vendor bill line Care creates in Odoo carries the external ID of its receipt line. Once the bill is
posted, its subtotal (excluding tax, after discount) is what the line cost. The line stores it per base unit:
subtotal / supplied_item_quantity, free units included. The costs come straight from Odoo
(sync_valuation_costs) or from a CSV (import_valuation_costs).
"""

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from urllib.parse import urlencode
from uuid import UUID

from care.emr.models.supply_delivery import SupplyDelivery
from django.db import transaction

from care_odoo.connector.connector import OdooConnector
from care_odoo.extensions import VALUATION_COST

ODOO_BILL = "odoo_bill"
# Bill lines asked from Odoo per request
PAGE_SIZE = 2000


@dataclass
class CostChanges:
    """New costs by receipt line id (None takes the line's cost off), and the costs that can't be stored."""

    lines: int = 0
    costs: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)
    # (receipt line external ID, quantity Care sent to the bill, quantity the bill received)
    quantity_differences: list = field(default_factory=list)

    @property
    def changed(self):
        return sum(1 for cost in self.costs.values() if cost is not None)

    @property
    def removed(self):
        return sum(1 for cost in self.costs.values() if cost is None)

    def save(self):
        """Write the costs onto the lines as they are now, keeping anything else saved since they were read."""
        with transaction.atomic():
            lines = SupplyDelivery.objects.select_for_update().in_bulk(list(self.costs))
            for line in lines.values():
                extensions = dict(line.extensions or {})
                if self.costs[line.id] is None:
                    extensions.pop(VALUATION_COST, None)
                else:
                    extensions[VALUATION_COST] = self.costs[line.id]
                line.extensions = extensions
            # bulk_update skips save hooks, so there is no Odoo resync or modified_date change.
            SupplyDelivery.objects.bulk_update(lines.values(), ["extensions"], batch_size=2000)


def cost_changes(subtotals, references, source, received=None):
    """
    The costs that differ from what the lines store, from subtotals and references keyed by receipt line
    external ID. With `received`, the quantity each line's bill received, also notes where Care sent another.
    """
    changes = CostChanges(lines=len(subtotals))
    line_ids = []
    for line_id in subtotals:
        try:
            UUID(str(line_id))
        except ValueError:
            changes.problems.append(f"{line_id}: not a Care receipt line ID")
        else:
            line_ids.append(line_id)
    lines = SupplyDelivery.objects.in_bulk(line_ids, field_name="external_id")
    lines = {str(key): line for key, line in lines.items()}
    for line_id in line_ids:
        line = lines.get(line_id)
        if line is None or not line.supplied_item_quantity:
            changes.problems.append(f"{line_id}: no Care receipt line with a quantity")
            continue
        cost = {
            "unit_cost": str((subtotals[line_id] / line.supplied_item_quantity).quantize(Decimal("0.000001"))),
            "source": source,
            "reference": ", ".join(references[line_id]),
        }
        if (line.extensions or {}).get(VALUATION_COST) != cost:
            changes.costs[line.id] = cost
        # The bill was built from the pack quantity, or from the base quantity without one
        sent = line.supplied_item_pack_quantity or line.supplied_item_quantity
        if received is not None and received[line_id] != sent:
            changes.quantity_differences.append((line_id, sent, received[line_id]))
    return changes


def posted_bill_lines():
    """Every line of a posted Odoo vendor bill that came from a Care receipt line."""
    bill_lines, after_id = [], 0
    while after_id is not None:
        query = urlencode({"after_id": after_id, "limit": PAGE_SIZE})
        # Pages are too large to log in full
        page = OdooConnector.call_api(f"api/account/move/vendor-bill/lines?{query}", {}, "GET", log_response=False)
        bill_lines += page["lines"]
        after_id = page["next_after_id"]
    return bill_lines


def bill_cost_changes():
    """
    The cost of every posted Odoo vendor bill line for its receipt line, and the costs to take off lines whose
    bill is no longer posted (reset to draft or cancelled). Bill lines for the same receipt line are added together.
    """
    bill_lines = posted_bill_lines()
    if not bill_lines:
        # Every cost would be taken off: a misconfigured Odoo is likelier than no posted bill at all
        raise ValueError("Odoo returned no posted vendor bill lines")
    subtotals, received, references = defaultdict(Decimal), defaultdict(Decimal), defaultdict(list)
    for bill_line in bill_lines:
        line_id = bill_line["care_line_id"]
        subtotals[line_id] += Decimal(str(bill_line["price_subtotal"]))
        received[line_id] += Decimal(str(bill_line["received_qty"]))
        references[line_id].append(bill_line["bill"])
    changes = cost_changes(subtotals, references, ODOO_BILL, received)
    billed = set(subtotals)
    stored = SupplyDelivery.objects.filter(**{f"extensions__{VALUATION_COST}__source": ODOO_BILL})
    for line_id, external_id in stored.values_list("id", "external_id"):
        if str(external_id) not in billed:
            changes.costs[line_id] = None
    return changes
