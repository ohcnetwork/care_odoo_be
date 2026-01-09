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
from care_odoo.resources.cash_transfer.spec import (
    AcceptTransferRequest,
    CancelTransferRequest,
    CreateTransferRequest,
    RejectTransferRequest,
    TransferData,
)

logger = logging.getLogger(__name__)


class CashTransferViewSet(EMRBaseViewSet):
    """
    ViewSet for managing cash transfers with Odoo.

    All endpoints are facility-scoped and use the authenticated user.

    URL Pattern: /facility/{facility_external_id}/cash-transfer/

    Endpoints:
    - GET / - List transfers (with optional filters)
    - POST / - Create a new transfer
    - PUT /{id}/accept/ - Accept a transfer
    - PUT /{id}/reject/ - Reject a transfer
    - PUT /{id}/cancel/ - Cancel a transfer (by sender)
    - GET /pending/ - Get pending incoming transfers
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

    def _serialize_transfer(self, transfer_data: dict) -> dict:
        """Serialize transfer data from Odoo response."""
        return TransferData(**transfer_data).model_dump()

    def list(self, request, facility_external_id=None):
        """
        List cash transfers for the facility with optional filters.

        GET /facility/{facility_external_id}/cash-transfer/

        Query Parameters:
        - status: Filter by transfer status (pending, accepted, rejected)
        - counter_x_care_id: Filter by counter (shows transfers to/from this counter)
        - from_session_id: Filter by the originating session ID
        """
        facility = self.get_facility_obj()
        transfer_status = request.query_params.get("status")
        counter_x_care_id = request.query_params.get("counter_x_care_id")
        from_session_id = request.query_params.get("from_session_id")

        query_params = {
            "facility_external_id": str(facility.external_id),
        }

        if transfer_status:
            query_params["status"] = transfer_status

        if counter_x_care_id:
            # Validate user has access to the counter if filtering by it
            location = self.validate_location_access(counter_x_care_id)
            query_params["counter_x_care_id"] = str(location.external_id)

        if from_session_id:
            query_params["from_session_id"] = from_session_id

        # Convert query_params to URL parameters
        url_params = "&".join([f"{key}={value}" for key, value in query_params.items()])
        api_url = f"api/care/cash/transfer/list?{url_params}"

        logger.info(
            "Listing cash transfers for facility %s: %s",
            facility.name,
            query_params,
        )

        try:
            response = OdooConnector.call_api(api_url, {}, "GET")

            transfers = response.get("transfers", [])
            serialized_transfers = [self._serialize_transfer(transfer) for transfer in transfers]

            return Response(
                {"success": True, "transfers": serialized_transfers},
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            logger.exception("Error listing cash transfers: %s", str(e))
            raise ValidationError(f"Error listing cash transfers: {str(e)}") from e

    def create(self, request, facility_external_id=None):
        """
        Create a new cash transfer from the authenticated user's session.

        POST /facility/{facility_external_id}/cash-transfer/
        {
            "from_counter_x_care_id": "UUID",
            "to_session_id": "UUID",
            "amount": 40000.00,
            "denominations": {"500": 50, "200": 75}
        }
        """
        try:
            request_data = CreateTransferRequest(**request.data)
        except Exception as e:
            raise ValidationError(f"Invalid request data: {str(e)}") from e

        user = request.user
        facility = self.get_facility_obj()
        from_location = self.validate_location_access(request_data.from_counter_x_care_id)
        # Build payload for Odoo
        data = {
            "from_user_id": str(user.external_id),
            "facility_external_id": str(facility.external_id),
            "from_counter_x_care_id": str(from_location.external_id),
            "to_session_id": request_data.to_session_id,
            "amount": request_data.amount,
            "created_by_ext_id": str(user.external_id),
            "created_by_name": user.full_name,
            "denominations": request_data.denominations,
        }

        logger.info(
            "Creating cash transfer for user %s at facility %s: %s",
            user.username,
            facility.name,
            data,
        )

        try:
            response = OdooConnector.call_api("api/care/cash/transfer", data, "POST")

            if not response.get("success", False):
                raise ValidationError(response.get("message", "Failed to create transfer in Odoo"))

            return Response(
                {
                    "success": True,
                    "transfer": self._serialize_transfer(response.get("transfer", {})),
                },
                status=status.HTTP_201_CREATED,
            )
        except ValidationError:
            raise
        except Exception as e:
            logger.exception("Error creating cash transfer: %s", str(e))
            raise ValidationError(f"Error creating cash transfer: {str(e)}") from e

    @action(detail=True, methods=["put", "post"], url_path="accept")
    def accept_transfer(self, request, pk=None, facility_external_id=None):
        """
        Accept an incoming cash transfer.

        PUT /facility/{facility_external_id}/cash-transfer/{id}/accept/
        {
            "counter_x_care_id": "UUID"  # The counter where user is accepting
        }
        """
        if not pk:
            raise ValidationError("Transfer ID is required")

        try:
            request_data = AcceptTransferRequest(**request.data)
        except Exception as e:
            raise ValidationError(f"Invalid request data: {str(e)}") from e

        user = request.user
        facility = self.get_facility_obj()

        # Validate user has access to the destination counter
        location = self.validate_location_access(request_data.counter_x_care_id)

        # Build payload for Odoo - user info derived from authenticated user
        data = {
            "facility_external_id": str(facility.external_id),
            "counter_x_care_id": str(location.external_id),
            "resolved_by_ext_id": str(user.external_id),
            "resolved_by_name": user.full_name,
        }

        logger.info(
            "Accepting transfer %s by user %s at facility %s",
            pk,
            user.username,
            facility.name,
        )

        try:
            response = OdooConnector.call_api(f"api/care/cash/transfer/{pk}/accept", data, "PUT")

            if not response.get("success", False):
                raise ValidationError(response.get("message", "Failed to accept transfer in Odoo"))

            return Response(
                {
                    "success": True,
                    "transfer": self._serialize_transfer(response.get("transfer", {})),
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError:
            raise
        except Exception as e:
            logger.exception("Error accepting cash transfer: %s", str(e))
            raise ValidationError(f"Error accepting cash transfer: {str(e)}") from e

    @action(detail=True, methods=["put", "post"], url_path="reject")
    def reject_transfer(self, request, pk=None, facility_external_id=None):
        """
        Reject an incoming cash transfer.

        PUT /facility/{facility_external_id}/cash-transfer/{id}/reject/
        {
            "counter_x_care_id": "UUID",  # The counter where user is rejecting
            "reason": "Amount doesn't match"  # Optional
        }
        """
        if not pk:
            raise ValidationError("Transfer ID is required")

        try:
            request_data = RejectTransferRequest(**request.data)
        except Exception as e:
            raise ValidationError(f"Invalid request data: {str(e)}") from e

        user = request.user
        facility = self.get_facility_obj()

        # Validate user has access to the destination counter
        location = self.validate_location_access(request_data.counter_x_care_id)

        # Build payload for Odoo - user info derived from authenticated user
        data = {
            "facility_external_id": str(facility.external_id),
            "counter_x_care_id": str(location.external_id),
            "resolved_by_ext_id": str(user.external_id),
            "resolved_by_name": user.full_name,
            "reason": request_data.reason,
        }

        logger.info(
            "Rejecting transfer %s by user %s at facility %s",
            pk,
            user.username,
            facility.name,
        )

        try:
            response = OdooConnector.call_api(f"api/care/cash/transfer/{pk}/reject", data, "PUT")

            if not response.get("success", False):
                raise ValidationError(response.get("message", "Failed to reject transfer in Odoo"))

            return Response(
                {
                    "success": True,
                    "transfer": self._serialize_transfer(response.get("transfer", {})),
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError:
            raise
        except Exception as e:
            logger.exception("Error rejecting cash transfer: %s", str(e))
            raise ValidationError(f"Error rejecting cash transfer: {str(e)}") from e

    @action(detail=True, methods=["put", "post"], url_path="cancel")
    def cancel_transfer(self, request, pk=None, facility_external_id=None):
        """
        Cancel a pending cash transfer (by sender).

        PUT /facility/{facility_external_id}/cash-transfer/{id}/cancel/
        {
            "counter_x_care_id": "UUID",  # The counter from which transfer was initiated
            "reason": "Transfer created by mistake"  # Optional
        }
        """
        if not pk:
            raise ValidationError("Transfer ID is required")

        try:
            request_data = CancelTransferRequest(**request.data)
        except Exception as e:
            raise ValidationError(f"Invalid request data: {str(e)}") from e

        user = request.user
        facility = self.get_facility_obj()

        # Validate user has access to the source counter
        location = self.validate_location_access(request_data.counter_x_care_id)

        # Build payload for Odoo - user info derived from authenticated user
        data = {
            "facility_external_id": str(facility.external_id),
            "counter_x_care_id": str(location.external_id),
            "cancelled_by_ext_id": str(user.external_id),
            "cancelled_by_name": user.full_name,
            "reason": request_data.reason,
        }

        logger.info(
            "Cancelling transfer %s by user %s at facility %s",
            pk,
            user.username,
            facility.name,
        )

        try:
            response = OdooConnector.call_api(f"api/care/cash/transfer/{pk}/cancel", data, "PUT")

            if not response.get("success", False):
                raise ValidationError(response.get("message", "Failed to cancel transfer in Odoo"))

            return Response(
                {
                    "success": True,
                    "transfer": self._serialize_transfer(response.get("transfer", {})),
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError:
            raise
        except Exception as e:
            logger.exception("Error cancelling cash transfer: %s", str(e))
            raise ValidationError(f"Error cancelling cash transfer: {str(e)}") from e

    @action(detail=False, methods=["get"], url_path="pending")
    def pending_transfers(self, request, facility_external_id=None):
        """
        Get pending incoming transfers at a location.

        GET /facility/{facility_external_id}/cash-transfer/pending/?counter_x_care_id=UUID

        Returns transfers pending at the specified counter. User must have access to the counter.
        """
        counter_x_care_id = request.query_params.get("counter_x_care_id")

        if not counter_x_care_id:
            raise ValidationError("counter_x_care_id query parameter is required")

        facility = self.get_facility_obj()
        user = request.user

        # Validate user has access to this counter
        location = self.validate_location_access(counter_x_care_id)

        # Filter by counter - transfers are location-based, not user-based
        query_params = {
            "facility_external_id": str(facility.external_id),
            "external_user_id": str(user.external_id),
            "counter_x_care_id": str(location.external_id),
        }

        logger.info(
            "Getting pending transfers for counter %s at facility %s",
            location.name,
            facility.name,
        )

        try:
            response = OdooConnector.call_api("api/care/cash/transfer/pending/", query_params, "POST")

            transfers = response.get("transfers", [])
            serialized_transfers = [self._serialize_transfer(transfer) for transfer in transfers]

            return Response(
                {"success": True, "transfers": serialized_transfers},
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            logger.exception("Error getting pending transfers: %s", str(e))
            raise ValidationError(f"Error getting pending transfers: {str(e)}") from e
