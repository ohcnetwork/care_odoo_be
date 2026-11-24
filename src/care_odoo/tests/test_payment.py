from unittest import mock

from django.test import SimpleTestCase

from care_odoo.resources.account_move_payment.payment import OdooPaymentResource


class PaymentCancelTests(SimpleTestCase):
    def cancel(self, issuer_type):
        payment = mock.Mock(external_id="payment-1", status="cancelled", issuer_type=issuer_type)
        with (
            mock.patch(
                "care_odoo.resources.account_move_payment.payment.PaymentReconciliation.objects.get",
                return_value=payment,
            ),
            mock.patch(
                "care_odoo.connector.connector.OdooConnector.call_api",
                return_value={"payment": {"id": 7}},
            ) as call_api,
        ):
            result = OdooPaymentResource().sync_payment_cancel_to_odoo_api("payment-1")
        return result, call_api

    def test_insurance_payment_is_not_cancelled_in_odoo(self):
        # Insurance payments are never sent to Odoo, so Odoo would answer "No payment found"
        result, call_api = self.cancel("insurer")
        self.assertIsNone(result)
        call_api.assert_not_called()

    def test_other_payments_are_cancelled_in_odoo(self):
        result, call_api = self.cancel("patient")
        self.assertEqual(result, 7)
        call_api.assert_called_once_with(
            "api/account/move/payment/cancel", {"x_care_id": "payment-1", "reason": "cancelled"}
        )
