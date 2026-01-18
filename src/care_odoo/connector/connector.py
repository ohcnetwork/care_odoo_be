import base64
import json
import logging

import requests

from care_odoo.exceptions import OdooClientError, OdooConnectionError, OdooServerError
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

        # Log curl equivalent for debugging
        try:
            headers_str = " ".join([f"-H '{k}: {v}'" for k, v in headers.items()])
            data_str = f"-d '{json.dumps(data)}'" if data else ""
            curl_command = f"curl -X {method} {headers_str} {data_str} '{url}'"
            logger.info("Equivalent curl command:\n%s", curl_command)
        except Exception as e:
            logger.info(e)

        try:
            response = requests.request(method, url, headers=headers, json=data, timeout=30)
            logger.info("Odoo API Response Status Code: %s", url)
            logger.info("Odoo API Response Status: %s", response.status_code)
            logger.info("Odoo API Raw Response: %s", response.text)

            response_json = response.json()
            logger.info("Odoo API Response JSON: %s", response_json)

            if not response.ok:
                error_msg = response_json.get("message", str(response.reason))
                logger.error("Odoo API Response Error: %s", error_msg)
                if response.status_code >= 500:
                    raise OdooServerError(error_msg)
                raise OdooClientError(error_msg)

            return response_json
        except requests.exceptions.Timeout as e:
            logger.exception("Odoo API Timeout: %s", str(e))
            raise OdooConnectionError("Odoo request timed out") from e
        except requests.exceptions.ConnectionError as e:
            logger.exception("Odoo API Connection Error: %s", str(e))
            raise OdooConnectionError("Could not connect to Odoo") from e
        except requests.exceptions.RequestException as e:
            logger.exception("Odoo API Request Error: %s", str(e))
            raise OdooConnectionError(str(e)) from e
