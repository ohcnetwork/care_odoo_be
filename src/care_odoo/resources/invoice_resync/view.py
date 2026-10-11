"""
Superuser endpoint to resync issued invoices to Odoo.

Care builds each invoice the way the live sync does and sends them to the Odoo connector's resync
endpoint in one call. Odoo corrects a posted invoice in place where its lines differ (same number,
payments re-matched) and leaves one that matches untouched. scripts/invoice_resync.py runs it for a
list of invoices.
"""

from care.emr.models.invoice import Invoice
from care.emr.resources.invoice.spec import InvoiceStatusOptions
from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from care_odoo.connector.connector import OdooConnector
from care_odoo.resources.account_move.invoice import OdooInvoiceResource

# Each request is one Odoo call, so this keeps it well inside the connector's and the server's 30s timeouts
MAX_INVOICES = 10
SYNCED_STATUSES = (InvoiceStatusOptions.issued.value, InvoiceStatusOptions.balanced.value)


class InvoiceResyncRequest(BaseModel):
    invoice_numbers: list[str] = Field(min_length=1, max_length=MAX_INVOICES)
    dry_run: bool = True
    all_or_nothing: bool = False


class IsSuperUser(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)


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
    POST {"invoice_numbers": [...], "dry_run": true, "all_or_nothing": false}, with up to MAX_INVOICES
    invoices. Dry run unless dry_run is false.
    """

    permission_classes = [IsSuperUser]

    def post(self, request, *args, **kwargs):
        try:
            params = InvoiceResyncRequest.model_validate(request.data)
        except PydanticValidationError as error:
            raise ValidationError(
                [f"{'.'.join(map(str, e['loc'])) or 'body'}: {e['msg']}" for e in error.errors()]
            ) from error
        invoices, results = invoices_from_care(params.invoice_numbers)
        if params.all_or_nothing and results:
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
        if invoices:
            results += send_to_odoo(invoices, params.dry_run, params.all_or_nothing)
        return Response({"dry_run": params.dry_run, "all_or_nothing": params.all_or_nothing, "results": results})
