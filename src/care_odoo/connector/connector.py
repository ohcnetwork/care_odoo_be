import base64
import logging

import requests
from rest_framework.exceptions import ValidationError

from care_odoo.settings import plugin_settings

logger = logging.getLogger(__name__)


class OdooConnector:
    @classmethod
    def call_api(cls, endpoint: str, data: dict, method: str = "POST") -> dict:
        """Call a custom Odoo addon API endpoint.

        Args:
            endpoint: The API endpoint path (e.g. '/api/create_invoice')
            data: The data to send in the request body

        Returns:
            dict: The JSON response from the API
        """
        # Include database name in credentials for Odoo session authentication
        auth = base64.b64encode(
            f"{plugin_settings.CARE_ODOO_USERNAME}:{plugin_settings.CARE_ODOO_PASSWORD}".encode()
        ).decode()

        # digital ocean
        # url = f"https://odoo.ohc.network/{endpoint}"

        # local
        # url = f"http://host.docker.internal:8069/{endpoint}"

        url = f"{plugin_settings.CARE_ODOO_PROTOCOL}://{plugin_settings.CARE_ODOO_HOST}"
        if plugin_settings.CARE_ODOO_PORT:
            url += f":{plugin_settings.CARE_ODOO_PORT}"
        url += f"/{endpoint}"
        headers = {
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
            "db": plugin_settings.CARE_ODOO_DATABASE,
        }

        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                json=data,
                timeout=plugin_settings.CARE_ODOO_REQUEST_TIMEOUT,
            )
            logger.info("Odoo API %s %s returned %s", method, endpoint, response.status_code)

            response_json = response.json()

            if not response.ok:
                error_msg = response_json.get("message", str(response.reason))
                logger.exception("Odoo API Response Error: %s", error_msg)
                raise ValidationError(str(error_msg))

            return response_json
        except requests.exceptions.RequestException as e:
            logger.exception("Odoo API Resonse Processing Error: %s", str(e))
            raise ValidationError(str(e)) from e
