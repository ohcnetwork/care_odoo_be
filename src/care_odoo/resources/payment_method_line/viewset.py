from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from care.emr.api.viewsets.base import EMRBaseViewSet
from care_odoo.connector.connector import OdooConnector
from care_odoo.resources.payment_method_line.spec import PaymentMethodLineData


class PaymentMethodLineViewSet(EMRBaseViewSet):
    """
    ViewSet for fetching payment method lines from Odoo.

    Payment method lines represent available payment sources for Care of Accounts
    (charity, sponsor, fund payments on behalf of patients).

    Usage:
        GET /api/v1/odoo/payment-method-line/
        GET /api/v1/odoo/payment-method-line/?journal_type=credit
    """

    def _build_query_params(self, request):
        """Build query parameters for Odoo API from request."""
        query_params = {}

        # journal_type defaults to 'credit' for Care of Accounts
        journal_type = request.GET.get("journal_type", "credit")
        query_params["journal_type"] = journal_type

        return query_params

    def list(self, request):
        """
        List payment method lines from Odoo filtered by journal type.

        Query Parameters:
            journal_type (str): The care connector code to filter by.
                              Default: 'credit' for Care of Accounts.

        Returns:
            List of payment method lines with id, name, code, journal_id, journal_name
        """
        query_params = self._build_query_params(request)

        try:
            # Call Odoo API to list payment method lines
            response = OdooConnector.call_api(
                "api/payment/method/lines",
                query_params,
                "GET",
            )

            # Extract payment methods from response
            payment_methods = response.get("payment_methods", [])

            # Serialize using PaymentMethodLineData spec
            serialized_payment_methods = []
            for pm in payment_methods:
                payment_method_data = PaymentMethodLineData(**pm)
                serialized_payment_methods.append(payment_method_data.model_dump())

            return Response(serialized_payment_methods)

        except Exception as e:
            raise ValidationError(
                f"Error fetching payment method lines from Odoo: {str(e)}"
            ) from e

    def retrieve(self, request, pk=None):
        """
        Retrieve a specific payment method line by ID.

        Path Parameters:
            pk (int): The ID of the payment method line

        Returns:
            Payment method line details
        """
        try:
            response = OdooConnector.call_api(
                f"api/payment/method/lines/{pk}",
                {},
                "GET",
            )

            payment_method = response.get("payment_method", {})
            payment_method_data = PaymentMethodLineData(**payment_method)

            return Response(payment_method_data.model_dump())

        except Exception as e:
            raise ValidationError(
                f"Error fetching payment method line from Odoo: {str(e)}"
            ) from e
