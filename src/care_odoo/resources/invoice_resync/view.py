"""
Superuser endpoint to resync issued invoices to Odoo.

Care builds each invoice the way the live sync does and sends them to the Odoo connector's resync
endpoint in one call. Odoo corrects a posted invoice in place where its lines differ (same number,
payments re-matched) and leaves one that matches untouched. scripts/invoice_resync.py runs it for a
list of invoices.
"""

from care.emr.models.invoice import Invoice
from care.emr.resources.invoice.spec import InvoiceStatusOptions
from pydantic import BaseModel, ConfigDict, Field
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
    # Unknown fields are rejected, so a request still sending dry_run fails instead of applying
    model_config = ConfigDict(extra="forbid")
    invoice_numbers: list[str] = Field(min_length=1, max_length=MAX_INVOICES)


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
        matches = list(Invoice.objects.filter(number=number).select_related("facility", "patient"))
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
        invoices.append({"number": number, "care_total": float(invoice.total_gross), "data": data})
    return invoices, problems


def send_to_odoo(invoices: list[dict]) -> list[dict]:
    try:
        response = OdooConnector.call_api(
            "api/account/move/resync",
            {"invoices": invoices},
        )
        return response["results"]
    except ValidationError as error:  # the connector raises this for HTTP and connection errors
        detail = error.detail[0] if isinstance(error.detail, list) else error.detail
        # A call that timed out may still have been applied in Odoo
        status = f"error: {detail} (running it again is safe: an invoice already corrected shows in sync)"
        return [
            {"invoice": invoice["number"], "care_total": invoice["care_total"], "status": status}
            for invoice in invoices
        ]


class InvoiceResyncView(APIView):
    """
    POST {"invoice_numbers": [...]}, with up to MAX_INVOICES invoices.
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
        if invoices:
            results += send_to_odoo(invoices)
        return Response({"results": results})
