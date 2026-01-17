from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from care_odoo.connector.connector import OdooConnector
from care.emr.api.viewsets.base import EMRBaseViewSet
from care_odoo.resources.payment_method.spec import SponsorData


class SponsorViewSet(EMRBaseViewSet):
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
        List sponsors from Odoo with filtering and search.
        """
        query_params = self._build_query_params(request)

        try:
            # Call Odoo API to list sponsors
            response = OdooConnector.call_api("api/sponsors/search", query_params, "GET")

            # Extract sponsors from response
            sponsors = response.get("sponsors", [])

            # Serialize sponsors using SponsorData spec
            serialized_sponsors = []
            for sponsor in sponsors:
                sponsor_data = SponsorData(**sponsor)
                serialized_sponsors.append(sponsor_data.model_dump())

            return Response(serialized_sponsors)
        except Exception as e:
            raise ValidationError(f"Error fetching sponsors from Odoo: {str(e)}") from e
