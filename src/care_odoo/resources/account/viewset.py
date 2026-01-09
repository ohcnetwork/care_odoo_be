import logging

from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from care.emr.models.account import Account

from care_odoo.apps import PLUGIN_NAME
from care_odoo.connector.connector import OdooConnector
from care.emr.api.viewsets.base import EMRBaseViewSet
from care_odoo.resources.payment_method.spec import PaymentMethodData, SetOdooPaymentMethodRequest

logger = logging.getLogger(__name__)


class AccountViewSet(EMRBaseViewSet):
    """
    ViewSet for managing account integrations with Odoo.
    """

    @action(detail=True, methods=["post"], url_path="set-odoo-payment-method")
    def set_odoo_payment_method(self, request, pk=None):
        try:
            request_data = SetOdooPaymentMethodRequest(**request.data)
        except Exception as e:
            raise ValidationError(f"Invalid request data: {str(e)}") from e

        try:
            account = Account.objects.get(external_id=pk)
        except Account.DoesNotExist as err:
            raise NotFound(f"Account with ID {pk} not found") from err

        # Initialize meta if None
        if account.meta is None:
            account.meta = {}

        if request_data.odoo_payment_method_id is not None:
            # Set the Odoo payment method ID
            if PLUGIN_NAME not in account.meta:
                account.meta[PLUGIN_NAME] = {}

            account.meta[PLUGIN_NAME]["odoo_payment_method_id"] = request_data.odoo_payment_method_id
            account.save(update_fields=["meta"])

            logger.info("Set Odoo payment method ID %s for Care account %s", request_data.odoo_payment_method_id, pk)

            return Response(
                {
                    "care_account_id": str(pk),
                    "odoo_payment_method_id": request_data.odoo_payment_method_id,
                    "message": "Odoo payment method ID set successfully",
                },
                status=status.HTTP_200_OK,
            )

        # Unset the Odoo payment method ID
        if PLUGIN_NAME in account.meta and "odoo_payment_method_id" in account.meta.get(PLUGIN_NAME, {}):
            del account.meta[PLUGIN_NAME]["odoo_payment_method_id"]

            # Clean up empty plugin dict
            if not account.meta[PLUGIN_NAME]:
                del account.meta[PLUGIN_NAME]

            account.save(update_fields=["meta"])
            logger.info("Unset Odoo payment method ID for Care account %s", pk)

        return Response(
            {
                "care_account_id": str(pk),
                "odoo_payment_method_id": None,
                "message": "Odoo payment method ID removed successfully",
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="get-odoo-payment-method")
    def get_odoo_payment_method(self, request, pk=None):
        try:
            account = Account.objects.get(external_id=pk)
        except Account.DoesNotExist as err:
            raise NotFound(f"Account with ID {pk} not found") from err

        # Get odoo_payment_method_id from meta
        odoo_payment_method_id = None
        if account.meta and PLUGIN_NAME in account.meta:
            odoo_payment_method_id = account.meta[PLUGIN_NAME].get("odoo_payment_method_id")

        if not odoo_payment_method_id:
            raise NotFound(f"No Odoo payment method linked for Account {pk}")

        try:
            # Call Odoo API to get payment method by ID
            response = OdooConnector.call_api(f"api/v1/payment/method/{odoo_payment_method_id}", {}, "GET")

            # Extract payment method from response
            payment_method = response.get("payment_method")
            if not payment_method:
                raise NotFound(f"Odoo payment method with ID {odoo_payment_method_id} not found")

            # Serialize payment method using PaymentMethodData spec
            payment_method_data = PaymentMethodData(**payment_method[0])

            return Response(payment_method_data.model_dump())
        except NotFound:
            raise
        except Exception as e:
            raise ValidationError(f"Error fetching payment method from Odoo: {str(e)}") from e
