from care.facility.models.facility import Facility
from django.http import HttpResponse
from rest_framework.decorators import api_view
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from care_odoo.resources.stock_valuation.valuation import end_of_day, value_inventory, write_csv


@api_view(["GET"])
def valuation_view(request):
    """GET ?facility=<uuid>&as_of=YYYY-MM-DD[&output=csv], valued at the end of that day, per batch and location."""
    if not request.user.is_superuser:
        raise PermissionDenied("Only superusers can read stock valuation")
    try:
        facility = Facility.objects.get(external_id=request.query_params["facility"])
        as_of = end_of_day(request.query_params["as_of"])
    except (KeyError, ValueError, Facility.DoesNotExist) as error:
        raise ValidationError("facility (uuid) and as_of (YYYY-MM-DD) are required") from error
    rows = value_inventory(facility, as_of)
    if request.query_params.get("output") == "csv":
        response = HttpResponse(content_type="text/csv")
        write_csv(rows, response)
        return response
    return Response({"as_of": as_of.isoformat(), "rows": rows})
