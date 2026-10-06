"""
Superuser endpoint to resync issued invoices to Odoo.

Care builds each invoice the way the live sync does and sends it to the Odoo connector's resync
endpoint. Odoo corrects a posted invoice in place where its lines differ (same number, payments
re-matched) and leaves one that matches untouched.
"""

import csv
import io

from care.emr.models.invoice import Invoice
from care.emr.resources.invoice.spec import InvoiceStatusOptions
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import BasePermission
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from care_odoo.connector.connector import OdooConnector
from care_odoo.resources.account_move.invoice import OdooInvoiceResource

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
    "lines_changed",
    "care_total",
    "old_untaxed",
    "new_untaxed",
    "old_tax",
    "new_tax",
    "old_total",
    "new_total",
    "old_payment_state",
    "new_payment_state",
    "changes",
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
    """A true/false field. Any other value is rejected, so a typo can't turn off a dry run."""
    if key not in data:
        return default
    value = data.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValidationError(f"{key} must be true or false.")


def invoices_from_care(numbers: list[str]) -> tuple[list[dict], list[dict]]:
    """
    Build each invoice the way the live sync does.

    Returns:
        The invoices to send, and a result for each number that can't be sent
    """
    resource = OdooInvoiceResource()
    invoices, problems = [], []
    for number in numbers:
        matches = list(Invoice.objects.filter(number=number, deleted=False).select_related("facility", "patient"))
        if len(matches) != 1:
            problems.append({"invoice": number, "status": f"not found in Care ({len(matches)} matching invoices)"})
            continue
        invoice = matches[0]
        if invoice.status not in SYNCED_STATUSES:
            problems.append({"invoice": number, "status": f"Care invoice is {invoice.status}"})
            continue
        try:
            data = resource.build_invoice_payload(invoice)
        except Exception as error:  # e.g. a missing price or encounter: report it and carry on
            problems.append({"invoice": number, "status": f"Care couldn't build the invoice: {error}"})
            continue
        invoices.append({"invoice": number, "care_total": float(invoice.total_gross), "data": data})
    return invoices, problems


def send_to_odoo(invoices: list[dict], dry_run: bool, all_or_nothing: bool) -> list[dict]:
    try:
        response = OdooConnector.call_api(
            "api/account/move/resync",
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


class InvoiceResyncView(APIView):
    """
    POST {"invoice_numbers": [...], "dry_run": true, "all_or_nothing": false}. Dry run unless dry_run
    is false. Add ?format=csv for the results as CSV.
    """

    permission_classes = [IsSuperUser]
    renderer_classes = [JSONRenderer, ResultsCSVRenderer]

    def post(self, request, *args, **kwargs):
        if not isinstance(request.data, dict):
            raise ValidationError("The body must be a JSON object.")
        dry_run = as_bool(request.data, "dry_run", default=True)
        all_or_nothing = as_bool(request.data, "all_or_nothing", default=False)
        numbers = request.data.get("invoice_numbers")
        if isinstance(numbers, str):
            numbers = [numbers]
        if not isinstance(numbers, list) or not all(isinstance(number, str) for number in numbers):
            raise ValidationError("invoice_numbers must be a list of invoice numbers.")
        invoices, results = invoices_from_care(numbers)

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
