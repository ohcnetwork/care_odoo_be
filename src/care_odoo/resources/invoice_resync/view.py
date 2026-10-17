"""
Superuser endpoint to resync an issued invoice to Odoo.

Care builds the invoice the way the live sync does and sends it to the Odoo connector's resync endpoint.
Odoo corrects the posted invoice in place if its lines differ (same number, payments re-matched) and
leaves it untouched if they match.
"""

from care.emr.models.invoice import Invoice
from care.emr.resources.invoice.spec import InvoiceStatusOptions
from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from care_odoo.connector.connector import OdooConnector
from care_odoo.resources.account_move.invoice import OdooInvoiceResource

SYNCED_STATUSES = (InvoiceStatusOptions.issued.value, InvoiceStatusOptions.balanced.value)


class InvoiceResyncRequest(BaseModel):
    # Unknown fields are rejected, so a request still sending dry_run fails instead of applying
    model_config = ConfigDict(extra="forbid")
    invoice_number: str


class IsSuperUser(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)


class InvoiceResyncView(APIView):
    """POST {"invoice_number": "..."}: resync that invoice to Odoo and return Odoo's result."""

    permission_classes = [IsSuperUser]

    def post(self, request, *args, **kwargs):
        try:
            params = InvoiceResyncRequest.model_validate(request.data)
        except PydanticValidationError as error:
            raise ValidationError(
                [f"{'.'.join(map(str, e['loc'])) or 'body'}: {e['msg']}" for e in error.errors()]
            ) from error
        number = params.invoice_number
        matches = list(Invoice.objects.filter(number=number).select_related("facility", "patient")[:2])
        if not matches:
            raise NotFound(f"No Care invoice is numbered {number}")
        if len(matches) > 1:
            raise ValidationError(f"More than one Care invoice is numbered {number}")
        invoice = matches[0]
        if invoice.status not in SYNCED_STATUSES:
            raise ValidationError(f"The Care invoice is {invoice.status}: only issued and balanced invoices are synced")
        try:
            data = OdooInvoiceResource().build_invoice_payload(invoice)
        except ValidationError:
            raise
        except Exception as error:  # e.g. a missing price
            raise ValidationError(f"Care couldn't build the invoice: {error}") from error
        response = OdooConnector.call_api(
            "api/account/move/resync",
            {"number": number, "care_total": float(invoice.total_gross), "data": data},
        )
        return Response(response["result"])
