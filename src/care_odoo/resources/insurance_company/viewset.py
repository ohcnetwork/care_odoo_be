from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from care_odoo.connector.connector import OdooConnector
from care.emr.api.viewsets.base import EMRBaseViewSet
from care_odoo.resources.insurance_company.spec import InsuranceCompanyData


class InsuranceCompanyViewSet(EMRBaseViewSet):
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
        List insurance companies from Odoo with filtering and search.
        """
        query_params = self._build_query_params(request)

        try:
            # Call Odoo API to list insurance companies
            response = OdooConnector.call_api("api/insurance/companies/search", query_params, "GET")

            # Extract insurance companies from response
            insurance_companies = response.get("insurance_companies", [])

            # Serialize insurance companies using InsuranceCompanyData spec
            serialized_insurance_companies = []
            for insurance_company in insurance_companies:
                insurance_company_data = InsuranceCompanyData(**insurance_company)
                serialized_insurance_companies.append(insurance_company_data.model_dump())

            return Response(serialized_insurance_companies)
        except Exception as e:
            raise ValidationError(f"Error fetching insurance companies from Odoo: {str(e)}") from e

