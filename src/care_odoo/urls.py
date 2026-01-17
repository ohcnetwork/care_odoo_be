from django.http import JsonResponse
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from care_odoo.resources.cash_session.viewset import CashSessionViewSet
from care_odoo.resources.cash_transfer.viewset import CashTransferViewSet
from care_odoo.resources.insurance_company.viewset import InsuranceCompanyViewSet
from care_odoo.resources.payment_method.viewset import SponsorViewSet


def ping(request):
    return JsonResponse({"status": "OK"})


router = DefaultRouter()
router.register("sponsor", SponsorViewSet, basename="sponsor")
router.register("insurance-company", InsuranceCompanyViewSet, basename="insurance-company")

# Facility-scoped router for cash management
facility_router = DefaultRouter()
facility_router.register("cash-session", CashSessionViewSet, basename="cash-session")
facility_router.register("cash-transfer", CashTransferViewSet, basename="cash-transfer")

urlpatterns = [
    path("ping/", ping, name="ping"),
    path("", include(router.urls)),
    path(
        "facility/<uuid:facility_external_id>/",
        include(facility_router.urls),
    ),
]
