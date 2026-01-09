import logging

from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from care.emr.models import FacilityLocation
from care.facility.models import Facility
from care.security.authorization import AuthorizationController
from care.utils.shortcuts import get_object_or_404

from care_odoo.connector.connector import OdooConnector
from care.emr.api.viewsets.base import EMRBaseViewSet
from care_odoo.resources.cash_session.spec import (
    CloseSessionRequest,
    CounterData,
    OpenSessionRequest,
    SessionData,
)

logger = logging.getLogger(__name__)


class CashSessionViewSet(EMRBaseViewSet):
    """
    ViewSet for managing cash sessions with Odoo.

    All endpoints are facility-scoped and use the authenticated user.

    URL Pattern: /facility/{facility_external_id}/cash-session/

    Endpoints:
    - POST / - Open a new session
    - PUT /close/ - Close current session
    - GET /current/ - Get current session for authenticated user at location
    - GET / - List sessions
    - GET /counters/ - List all counters with session status
    """

    def get_facility_obj(self) -> Facility:
        """Get facility from URL kwargs."""
        return get_object_or_404(Facility, external_id=self.kwargs["facility_external_id"])

    def get_location_obj(self, location_external_id: str) -> FacilityLocation:
        """Get location by external ID within the facility."""
        facility = self.get_facility_obj()
        try:
            location = FacilityLocation.objects.get(external_id=location_external_id, facility=facility)
            return location
        except FacilityLocation.DoesNotExist:
            raise NotFound(f"Location {location_external_id} not found in this facility")

    def validate_location_access(self, location_external_id: str) -> FacilityLocation:
        """
        Validate that the authenticated user has access to the location.

        Returns:
            FacilityLocation object if access is granted

        Raises:
            NotFound: If location doesn't exist in facility
            PermissionDenied: If user doesn't have access
        """
        facility = self.get_facility_obj()
        location = self.get_location_obj(location_external_id)

        if not AuthorizationController.call("can_list_facility_location_obj", self.request.user, facility, location):
            raise PermissionDenied(f"You do not have access to location {location.name}")

        return location

    def _serialize_session(self, session_data: dict) -> dict:
        """Serialize session data from Odoo response."""
        return SessionData(**session_data).model_dump()

    def _serialize_counter(self, counter_data: dict) -> dict:
        """Serialize counter data from Odoo response."""
        return CounterData(**counter_data).model_dump()

    def create(self, request, facility_external_id=None):
        """
        Open a new cash session for the authenticated user.

        POST /facility/{facility_external_id}/cash-session/
        {
            "counter_x_care_id": "UUID",
            "opening_balance": 5000.00
        }
        """
        try:
            request_data = OpenSessionRequest(**request.data)
        except Exception as e:
            raise ValidationError(f"Invalid request data: {str(e)}") from e

        user = request.user
        facility = self.get_facility_obj()
        location = self.validate_location_access(request_data.counter_x_care_id)

        # Build payload for Odoo
        data = {
            "external_user_id": str(user.external_id),
            "external_user_name": user.full_name,
            "counter_x_care_id": str(location.external_id),
            "opening_balance": request_data.opening_balance,
        }

        logger.info(
            "Opening cash session for user %s at facility %s: %s",
            user.username,
            facility.name,
            data,
        )

        try:
            response = OdooConnector.call_api("api/care/cash/session", data, "POST")

            if not response.get("success", False):
                raise ValidationError(response.get("message", "Failed to open session in Odoo"))

            return Response(
                {
                    "success": True,
                    "session": self._serialize_session(response.get("session", {})),
                },
                status=status.HTTP_201_CREATED,
            )
        except ValidationError:
            raise
        except Exception as e:
            logger.exception("Error opening cash session: %s", str(e))
            raise ValidationError(f"Error opening cash session: {str(e)}") from e

    @action(detail=False, methods=["put", "post"], url_path="close")
    def close_session(self, request, facility_external_id=None):
        """
        Close the current cash session for the authenticated user.

        PUT /facility/{facility_external_id}/cash-session/close/
        {
            "counter_x_care_id": "UUID",
            "declared_amount": 45000.00,
            "denominations": {"500": 50, "200": 100}
        }
        """
        try:
            request_data = CloseSessionRequest(**request.data)
        except Exception as e:
            raise ValidationError(f"Invalid request data: {str(e)}") from e

        user = request.user
        facility = self.get_facility_obj()
        location = self.validate_location_access(request_data.counter_x_care_id)

        # Build payload for Odoo
        data = {
            "external_user_id": str(user.external_id),
            "facility_external_id": str(facility.external_id),
            "counter_x_care_id": str(location.external_id),
            "closed_by_ext_id": str(user.external_id),
            "closed_by_name": user.full_name,
        }

        logger.info(
            "Closing cash session for user %s at facility %s: %s",
            user.username,
            facility.name,
            data,
        )

        try:
            response = OdooConnector.call_api("api/care/cash/session/close", data, "PUT")

            if not response.get("success", False):
                raise ValidationError(response.get("message", "Failed to close session in Odoo"))

            return Response(
                {
                    "success": True,
                    "session": self._serialize_session(response.get("session", {})),
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError:
            raise
        except Exception as e:
            logger.exception("Error closing cash session: %s", str(e))
            raise ValidationError(f"Error closing cash session: {str(e)}") from e

    @action(detail=False, methods=["post"], url_path="current")
    def current_session(self, request, facility_external_id=None):
        """
        Get the current open session for the authenticated user at a location.

        POST /facility/{facility_external_id}/cash-session/current/
        {
            "counter_x_care_id": "UUID"
        }
        """
        counter_x_care_id = request.data.get("counter_x_care_id")

        if not counter_x_care_id:
            raise ValidationError("counter_x_care_id is required")

        user = request.user
        facility = self.get_facility_obj()
        location = self.validate_location_access(counter_x_care_id)

        data = {
            "external_user_id": str(user.external_id),
            "counter_x_care_id": str(location.external_id),
        }

        logger.info(
            "Getting current session for user %s at facility %s: %s",
            user.username,
            facility.name,
            data,
        )

        try:
            response = OdooConnector.call_api("api/care/cash/session/current", data, "POST")

            session_data = response.get("session")
            if session_data:
                return Response(
                    {
                        "success": True,
                        "session": self._serialize_session(session_data),
                    },
                    status=status.HTTP_200_OK,
                )
            else:
                return Response(
                    {"success": True, "session": None, "message": "No open session"},
                    status=status.HTTP_200_OK,
                )
        except Exception as e:
            logger.exception("Error getting current cash session: %s", str(e))
            raise ValidationError(f"Error getting current cash session: {str(e)}") from e

    def list(self, request, facility_external_id=None):
        """
        List all sessions in the facility, optionally filtered by status.

        GET /facility/{facility_external_id}/cash-session/?status=open
        """
        facility = self.get_facility_obj()
        session_status = request.query_params.get("status")

        query_params = {
            "facility_external_id": str(facility.external_id),
            "external_user_id": str(request.user.external_id),
        }
        if session_status:
            query_params["status"] = session_status

        logger.info("Listing cash sessions for facility %s: %s", facility.name, query_params)

        # Convert query_params to URL parameters
        url_params = "&".join([f"{key}={value}" for key, value in query_params.items()])
        api_url = f"api/care/cash/session/list?{url_params}"

        try:
            response = OdooConnector.call_api(api_url, {}, "GET")

            sessions = response.get("sessions", [])
            serialized_sessions = [self._serialize_session(session) for session in sessions]

            return Response(
                {"success": True, "sessions": serialized_sessions},
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            logger.exception("Error listing cash sessions: %s", str(e))
            raise ValidationError(f"Error listing cash sessions: {str(e)}") from e

    @action(detail=False, methods=["get"], url_path="counters")
    def list_counters(self, request, facility_external_id=None):
        """
        List all available counters in the facility with their session status.

        GET /facility/{facility_external_id}/cash-session/counters/
        """
        facility = self.get_facility_obj()

        logger.info("Listing cash counters for facility %s", facility.name)

        try:
            response = OdooConnector.call_api("api/care/cash/counters", {}, "GET")

            counters = response.get("counters", [])
            serialized_counters = [self._serialize_counter(counter) for counter in counters]

            return Response(
                {
                    "success": True,
                    "counters": serialized_counters,
                    "count": len(serialized_counters),
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            logger.exception("Error listing cash counters: %s", str(e))
            raise ValidationError(f"Error listing cash counters: {str(e)}") from e
