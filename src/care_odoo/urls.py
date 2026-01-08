from django.http import JsonResponse
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_nested import routers as nested_routers

from care_odoo.resources.account.viewset import AccountViewSet
from care_odoo.resources.cash_session.viewset import CashSessionViewSet
from care_odoo.resources.cash_transfer.viewset import CashTransferViewSet
from care_odoo.resources.payment_method.viewset import PaymentMethodViewSet


def ping(request):
    return JsonResponse({"status": "OK"})

# TODO: @amjithtitus09 we need to add /v1/ in front of all the urls to be in line with the rest of the api.
# Main router for non-facility-scoped endpoints
router = DefaultRouter()
router.register("payment-method", PaymentMethodViewSet, basename="payment-method")
router.register("account", AccountViewSet, basename="account")

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
