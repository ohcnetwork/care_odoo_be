from django.http import JsonResponse
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from care_odoo.resources.cash_session.viewset import CashSessionViewSet
from care_odoo.resources.cash_transfer.viewset import CashTransferViewSet
from care_odoo.resources.insurance_company.viewset import InsuranceCompanyViewSet
from care_odoo.resources.payment_method_line.viewset import PaymentMethodLineViewSet


def ping(request):
    return JsonResponse({"status": "OK"})


router = DefaultRouter()
router.register("insurance-company", InsuranceCompanyViewSet, basename="insurance-company")
router.register("payment-method-line", PaymentMethodLineViewSet, basename="payment-method-line")

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
