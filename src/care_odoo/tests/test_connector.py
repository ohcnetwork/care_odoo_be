from unittest import mock

import requests
from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from care_odoo.connector.connector import OdooConnectionError, OdooConnector


def odoo_answer(status=200, body=None):
    answer = mock.Mock(ok=status < 400, status_code=status, reason="Bad Request", text="{}")
    answer.json.return_value = body or {}
    return answer


class CallApiTests(SimpleTestCase):
    def request(self, **kwargs):
        return mock.patch("care_odoo.connector.connector.requests.request", **kwargs)

    def body_sent(self, data, method):
        """The body requests puts on the wire for this call."""
        with self.request(return_value=odoo_answer()) as request:
            OdooConnector.call_api("api/endpoint", data, method)
        self.assertEqual(request.call_args.args[0], method)
        sent = requests.Request(method, "http://odoo.test/api/endpoint", json=request.call_args.kwargs["json"])
        return sent.prepare().body

    def test_get_sends_no_body(self):
        self.assertIsNone(self.body_sent({}, "GET"))
        # Odoo's GET routes read only the query string
        self.assertIsNone(self.body_sent({"journal_type": "credit"}, "GET"))

    def test_other_methods_send_their_data_as_before(self):
        self.assertEqual(self.body_sent({}, "POST"), b"{}")
        self.assertEqual(self.body_sent({"x_care_id": "1"}, "POST"), b'{"x_care_id": "1"}')
        self.assertEqual(self.body_sent({"reason": "late"}, "PUT"), b'{"reason": "late"}')

    def test_a_broken_connection_is_a_connection_error(self):
        for failure in (
            requests.exceptions.ChunkedEncodingError("Connection broken: Connection reset by peer"),
            requests.exceptions.ConnectionError("Connection refused"),
            requests.exceptions.ReadTimeout("Read timed out"),
        ):
            with (
                self.subTest(failure=type(failure).__name__),
                self.request(side_effect=failure),
                self.assertLogs("care_odoo.connector.connector", "ERROR"),
                self.assertRaises(OdooConnectionError) as raised,
            ):
                OdooConnector.call_api("api/endpoint", {}, "GET")
            # Still the ValidationError callers catch
            self.assertIsInstance(raised.exception, ValidationError)

    def test_odoo_refusing_is_not_a_connection_error(self):
        with (
            self.request(return_value=odoo_answer(400, {"message": "Invalid credentials"})),
            self.assertLogs("care_odoo.connector.connector", "ERROR"),
            self.assertRaisesMessage(ValidationError, "Invalid credentials") as raised,
        ):
            OdooConnector.call_api("api/endpoint", {}, "GET")
        self.assertNotIsInstance(raised.exception, OdooConnectionError)
