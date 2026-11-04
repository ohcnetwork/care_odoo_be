import csv
from datetime import datetime, time
from zoneinfo import ZoneInfo

from care.facility.models.facility import Facility
from django.http import HttpResponse
from rest_framework.decorators import api_view
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from care_odoo.resources.stock_valuation.valuation import value_inventory

COLUMNS = [
    "product_id",
    "product_name",
    "batch",
    "expiration_date",
    "quantity",
    "unit_cost",
    "value",
    "cost_sources",
    "flags",
]


def end_of_day(value, zone="Asia/Kolkata"):
    day = datetime.strptime(value, "%Y-%m-%d").date()  # noqa: DTZ007
    return datetime.combine(day, time.max, tzinfo=ZoneInfo(zone))


def write_csv(rows, handle):
    writer = csv.DictWriter(handle, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)


@api_view(["GET"])
def valuation_view(request):
    """GET ?facility=<uuid>&as_of=YYYY-MM-DD[&output=csv], end of day India time."""
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
