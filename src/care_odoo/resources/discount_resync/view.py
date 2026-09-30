"""
One-off API to correct Odoo invoices synced before the applied-discounts fix.

Before that fix the sync sent every discount a charge item was eligible for, and Odoo added them all
up. This sends each invoice's correct discounts to Odoo's resync endpoint, which fixes the invoice in
place (same number, payments re-matched) or leaves it untouched.
"""

import csv
import io
import json

from care.emr.models.charge_item import ChargeItem
from care.emr.models.invoice import Invoice
from care.emr.resources.invoice.spec import InvoiceStatusOptions
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import BasePermission
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from care_odoo.connector.connector import OdooConnector
from care_odoo.resources.utils import get_all_discounts

# Invoices per Odoo call, to stay well inside the connector's 30s timeout
ODOO_BATCH_SIZE = 5
# all_or_nothing sends the whole request to Odoo in one call
ALL_OR_NOTHING_LIMIT = 10
SYNCED_STATUSES = (InvoiceStatusOptions.issued.value, InvoiceStatusOptions.balanced.value)
RESULT_COLUMNS = [
    "invoice",
    "status",
    "invoice_date",
    "journal",
    "lines_fixed",
    "care_total",
    "old_untaxed",
    "new_untaxed",
    "old_tax",
    "new_tax",
    "old_total",
    "new_total",
    "old_payment_state",
    "new_payment_state",
]


class IsSuperUser(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)


class ResultsCSVRenderer(BaseRenderer):
    """Renders the results for ?format=csv."""

    media_type = "text/csv"
    format = "csv"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        rows = data["results"] if isinstance(data, dict) and "results" in data else [{"status": f"error: {data}"}]
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=RESULT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue()


def as_bool(data, key: str, default: bool) -> bool:
    """A true/false field from JSON or form data. Anything else is rejected, so a typo can't turn off a dry run."""
    value = data.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes"):
        return True
    if text in ("0", "false", "no"):
        return False
    raise ValidationError(f"{key} must be true or false.")


def invoices_from_care(numbers: list[str]) -> tuple[list[dict], list[dict]]:
    """
    Build the Odoo payload from Care, the same way the invoice sync does.

    Returns:
        The invoices to send, and a result for each number that can't be sent
    """
    invoices, problems = [], []
    for number in numbers:
        matches = list(Invoice.objects.filter(number=number, deleted=False))
        if len(matches) != 1:
            problems.append({"invoice": number, "status": f"not found in Care ({len(matches)} matching invoices)"})
            continue
        invoice = matches[0]
        if invoice.status not in SYNCED_STATUSES:
            problems.append({"invoice": number, "status": f"Care invoice is {invoice.status}"})
            continue
        lines = [
            {
                "x_care_id": str(charge_item.external_id),
                "discounts": [discount.model_dump(mode="json") for discount in get_all_discounts(charge_item) or []],
            }
            for charge_item in ChargeItem.objects.filter(paid_invoice=invoice).select_related("charge_item_definition")
            # The sync only sends items with a definition
            if charge_item.charge_item_definition
        ]
        invoices.append(
            {
                "invoice": number,
                "x_care_id": str(invoice.external_id),
                "care_total": float(invoice.total_gross),
                "lines": lines,
            }
        )
    return invoices, problems


def invoices_from_csv(upload) -> list[dict]:
    """Build the Odoo payload from the 01_care_find_affected.sql export."""
    invoices = {}
    for row in csv.DictReader(io.StringIO(upload.read().decode("utf-8-sig"))):
        invoice = invoices.setdefault(
            row["invoice"],
            {
                "invoice": row["invoice"],
                "x_care_id": row["invoice_x_care_id"],
                "care_total": float(row["care_total"].replace(",", "")),
                "lines": [],
            },
        )
        invoice["lines"].append(
            {"x_care_id": row["line_x_care_id"], "discounts": json.loads(row["applied_discounts"] or "[]")}
        )
    return list(invoices.values())


def send_to_odoo(invoices: list[dict], dry_run: bool, all_or_nothing: bool) -> list[dict]:
    try:
        response = OdooConnector.call_api(
            "api/account/move/resync_discounts",
            {"dry_run": dry_run, "all_or_nothing": all_or_nothing, "invoices": invoices},
        )
        return response["results"]
    except ValidationError as error:  # the connector raises this for HTTP and connection errors
        detail = error.detail[0] if isinstance(error.detail, list) else error.detail
        status = f"error: {detail}"
        if not dry_run:
            # A call that timed out may still have been applied in Odoo
            status += " (run a dry run for this invoice to check whether Odoo applied it)"
        return [
            {"invoice": invoice["invoice"], "care_total": invoice["care_total"], "status": status}
            for invoice in invoices
        ]


class DiscountResyncView(APIView):
    """
    POST {"invoice_numbers": [...], "dry_run": true, "all_or_nothing": false}, or upload the
    affected_invoice_lines.csv export as "file" (multipart). Dry run unless dry_run is false.
    Add ?format=csv for the results as CSV.
    """

    permission_classes = [IsSuperUser]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    renderer_classes = [JSONRenderer, ResultsCSVRenderer]

    def post(self, request, *args, **kwargs):
        dry_run = as_bool(request.data, "dry_run", default=True)
        all_or_nothing = as_bool(request.data, "all_or_nothing", default=False)
        if "file" in request.FILES:
            invoices, results = invoices_from_csv(request.FILES["file"]), []
        else:
            numbers = request.data.get("invoice_numbers") or []
            invoices, results = invoices_from_care([numbers] if isinstance(numbers, str) else numbers)

        if all_or_nothing:
            if len(invoices) + len(results) > ALL_OR_NOTHING_LIMIT:
                raise ValidationError(f"all_or_nothing supports up to {ALL_OR_NOTHING_LIMIT} invoices per call.")
            if results:
                # An invoice can't be sent, so send none of them
                results += [
                    {
                        "invoice": invoice["invoice"],
                        "care_total": invoice["care_total"],
                        "status": "not sent: another invoice in this request has a problem",
                    }
                    for invoice in invoices
                ]
                invoices = []

        batch_size = len(invoices) if all_or_nothing else ODOO_BATCH_SIZE
        for start in range(0, len(invoices), batch_size or 1):
            results += send_to_odoo(invoices[start : start + batch_size], dry_run, all_or_nothing)
        return Response({"dry_run": dry_run, "all_or_nothing": all_or_nothing, "results": results})
