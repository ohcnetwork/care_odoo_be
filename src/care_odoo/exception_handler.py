# TODO: Plugs will for sure have better error handling, so we need to find and replace this with that.
"""
Exception handler for care_odoo that follows Care's standard error format.

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

import json

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http.response import Http404
from pydantic import ValidationError as PydanticValidationError
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.fields import get_error_detail
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


def care_odoo_exception_handler(exc, context):
    """
    Custom exception handler that formats errors in Care's standard format:

    {
        "errors": [
            {"type": "<error_type>", "msg": "<error_message>"}
        ]
    }
    """
    # Convert Django ValidationError to DRF ValidationError
    if isinstance(exc, DjangoValidationError):
        exc = DRFValidationError(detail={"detail": get_error_detail(exc)[0]})

    # Handle Pydantic ValidationError
    if isinstance(exc, PydanticValidationError):
        return Response({"errors": json.loads(exc.json())}, status=400)

    # Handle Http404 (from get_object_or_404)
    if isinstance(exc, Http404):
        return Response(
            {
                "errors": [
                    {
                        "type": "object_not_found",
                        "msg": exc.args[0] if exc.args else "Object not found",
                    }
                ]
            },
            status=404,
        )

    # Handle DRF NotFound
    if isinstance(exc, NotFound):
        msg = str(exc.detail) if exc.detail else "Object not found"
        return Response(
            {"errors": [{"type": "object_not_found", "msg": msg}]},
            status=404,
        )

    # Handle PermissionDenied
    if isinstance(exc, DRFPermissionDenied):
        msg = str(exc.detail) if exc.detail else "Permission denied"
        return Response(
            {"errors": [{"type": "permission_denied", "msg": msg}]},
            status=403,
        )

    # Handle DRF ValidationError
    if isinstance(exc, DRFValidationError) and getattr(exc, "detail", None):
        # Check if already in our format
        if isinstance(exc.detail, dict) and "errors" in exc.detail:
            return Response(exc.detail, status=400)

        if isinstance(exc.detail, list):
            errors = " , ".join([str(e) for e in exc.detail])
            return Response(
                {"errors": [{"type": "validation_error", "msg": errors}]},
                status=400,
            )

        return Response(
            {"errors": [{"type": "validation_error", "msg": str(exc.detail)}]},
            status=400,
        )

    # Fall back to DRF's default exception handler
    return drf_exception_handler(exc, context)
