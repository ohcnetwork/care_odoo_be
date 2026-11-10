import base64
import json
import logging

import requests
from rest_framework.exceptions import ValidationError

from care_odoo.settings import plugin_settings

logger = logging.getLogger(__name__)

# Odoo couldn't be reached, didn't answer in time, or the connection broke while its answer was read
CONNECTION_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.Timeout,
)


class OdooConnectionError(ValidationError):
    """The connection to Odoo failed rather than the request. Asking again may work."""


class OdooConnector:
    @classmethod
    def call_api(cls, endpoint: str, data: dict, method: str = "POST", log_response: bool = True) -> dict:
        """Call a custom Odoo addon API endpoint.

        Args:
            endpoint: The API endpoint path (e.g. '/api/create_invoice')
            data: The data to send in the request body
            log_response: False to leave out the response body, for responses too large to log

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

        # Odoo's GET routes read only the query string. A body they leave unread on the socket can make the
        # server reset the connection, so GETs send none.
        body = None if method.upper() == "GET" else data

        # Log curl equivalent for debugging
        try:
            headers_str = " ".join([f"-H '{k}: {v}'" for k, v in headers.items()])
            data_str = f"-d '{json.dumps(body)}'" if body else ""
            curl_command = f"curl -X {method} {headers_str} {data_str} '{url}'"
            logger.info("Equivalent curl command:\n%s", curl_command)
        except Exception as e:
            logger.info(e)

        try:
            response = requests.request(method, url, headers=headers, json=body, timeout=30)
            logger.info("Odoo API Response Status Code: %s", url)
            logger.info("Odoo API Response Status: %s", response.status_code)
            if log_response:
                logger.info("Odoo API Raw Response: %s", response.text)

            response_json = response.json()
            if log_response:
                logger.info("Odoo API Response JSON: %s", response_json)

            if not response.ok:
                error_msg = response_json.get("message", str(response.reason))
                logger.exception("Odoo API Response Error: %s", error_msg)
                raise ValidationError(str(error_msg))

            return response_json
        except CONNECTION_ERRORS as e:
            logger.exception("Odoo API Connection Error: %s", str(e))
            raise OdooConnectionError(str(e)) from e
        except requests.exceptions.RequestException as e:
            logger.exception("Odoo API Resonse Processing Error: %s", str(e))
            raise ValidationError(str(e)) from e
