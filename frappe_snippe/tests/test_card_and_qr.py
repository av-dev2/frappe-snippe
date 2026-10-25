from unittest.mock import patch

import frappe
from snippe import Payment

from frappe_snippe.frappe_snippe.doctype.payment_collection import payment_collection
from frappe_snippe.tests.test_collection_customer import make_address
from frappe_snippe.tests.test_invoice_payment import (
	InvoicePaymentTestCase,
	make_client,
	make_draft_sales_invoice,
)
from frappe_snippe.tests.test_snippe_setting import PHONE_MODE, make_snippe_setting

CARD_URL = "https://checkout.evmak.com/pay/abc"


def make_card_and_qr_client():
	client = make_client()
	pending = {"status": "pending", "amount": {"value": 0, "currency": "TZS"}}
	client.create_card_payment.return_value = Payment.from_dict(
		{"reference": "pi_card", "payment_url": CARD_URL, **pending}
	)
	client.create_qr_payment.return_value = Payment.from_dict(
		{"reference": "pi_qr", "payment_qr_code": "iVBORw0KGgo=", **pending}
	)
	return client


class TestCardAndQrPayments(InvoicePaymentTestCase):
	def setUp(self):
		super().setUp()
		make_snippe_setting(card_redirect_url="https://shop.example/paid")
		make_address(frappe.get_doc("Customer", self.customer))
		self.invoice = make_draft_sales_invoice(self.customer, 9000)
		self.client = make_card_and_qr_client()

	def pay(self, payment_method):
		with patch.object(payment_collection, "get_snippe_client", return_value=self.client):
			return payment_collection.make_payment(
				"Sales Invoice",
				self.invoice.name,
				"0712345678",
				PHONE_MODE,
				9000,
				payment_method=payment_method,
			)

	def test_card_payment_gives_a_checkout_link_and_its_qr(self):
		progress = payment_collection.get_payment_progress(self.pay("Card"))

		self.assertEqual(
			self.client.create_card_payment.call_args.kwargs["redirect_url"], "https://shop.example/paid"
		)
		self.assertEqual((progress["status"], progress["payment_url"]), ("Pending", CARD_URL))
		self.assertTrue(progress["payment_url_qr_svg"])

	def test_qr_payment_gives_snippe_qr_image(self):
		progress = payment_collection.get_payment_progress(self.pay("Dynamic QR"))

		self.client.create_qr_payment.assert_called_once()
		self.assertEqual(
			(progress["payment_method"], progress["payment_qr_code"], progress["payment_url_qr_svg"]),
			("Dynamic QR", "iVBORw0KGgo=", None),
		)

	def test_mobile_money_stays_the_default(self):
		with patch.object(payment_collection, "get_snippe_client", return_value=self.client):
			name = payment_collection.make_payment(
				"Sales Invoice", self.invoice.name, "0712345678", PHONE_MODE, 9000
			)
		self.assertEqual(frappe.db.get_value("Payment Collection", name, "payment_method"), "Mobile Money")

	def test_hosted_session_is_not_a_dialog_method(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Payment Method must be one of"):
			self.pay("Hosted Session")

	def test_progress_needs_read_access_to_the_paid_invoice(self):
		collection = self.pay("Dynamic QR")
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				payment_collection.get_payment_progress(collection)
		finally:
			frappe.set_user("Administrator")
