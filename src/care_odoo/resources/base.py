"""
Base viewset for care_odoo that provides consistent error handling.
"""

from rest_framework import viewsets

from care_odoo.exception_handler import care_odoo_exception_handler


class CareOdooBaseViewSet(viewsets.ViewSet):
    """
    Base ViewSet for care_odoo that uses the standard Care error format.

    Error responses follow this structure:
    {
        "errors": [
            {
                "type": "object_not_found",
                "msg": "No Facility matches the given query."
            }
        ]
    }
    """

    def get_exception_handler(self):
        return care_odoo_exception_handler
