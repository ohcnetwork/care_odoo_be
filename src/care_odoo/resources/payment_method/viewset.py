from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from care_odoo.connector.connector import OdooConnector
from care_odoo.resources.base import CareOdooBaseViewSet
from care_odoo.resources.payment_method.spec import PaymentMethodData


class PaymentMethodViewSet(CareOdooBaseViewSet):
    def _build_query_params(self, request):
        """Build query parameters for Odoo API from request."""
        query_params = {}

        search_key = request.GET.get("search_key")
        if search_key:
            query_params["search_key"] = search_key
        else:
            query_params["search_key"] = ""

        return query_params

    def list(self, request):
        """
        List payment methods from Odoo with filtering and search.
        """
        query_params = self._build_query_params(request)

        try:
            # Call Odoo API to list payment methods
            response = OdooConnector.call_api("api/payment/methods/search", query_params, "GET")

            # Extract accounts from response
            payment_methods = response.get("payment_methods", [])

            # Serialize payment methods using PaymentMethodData spec
            serialized_payment_methods = []
            for payment_method in payment_methods:
                payment_method_data = PaymentMethodData(**payment_method)
                serialized_payment_methods.append(payment_method_data.model_dump())

            return Response(serialized_payment_methods)
        except Exception as e:
            raise ValidationError(f"Error fetching accounts from Odoo: {str(e)}") from e
