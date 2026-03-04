from rest_framework import status
from rest_framework.exceptions import APIException


class OdooConnectionError(APIException):
    """Raised when unable to connect to Odoo (network issues, timeouts)."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Unable to connect to Odoo service."
    default_code = "odoo_connection_error"


class OdooServerError(APIException):
    """Raised when Odoo returns a 5xx error."""

    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "Odoo service returned an error."
    default_code = "odoo_server_error"


class OdooClientError(APIException):
    """Raised when Odoo returns a 4xx error (bad request to Odoo)."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "Invalid request to Odoo service."
    default_code = "odoo_client_error"
