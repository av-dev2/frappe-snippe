from unittest.mock import patch

import frappe
from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request

from frappe_snippe.api import payment_gateway, webhooks
from frappe_snippe.frappe_snippe.doctype.payment_collection import payment_collection
from frappe_snippe.tests.test_collection_customer import make_customer
from frappe_snippe.tests.test_collection_reconcile import make_client as make_reply_client
from frappe_snippe.tests.test_paid_amount import make_sales_invoice
from frappe_snippe.tests.test_snippe_setting import (
	TZS_CASH,
	TZS_COMPANY,
	TZS_FEES,
	TzsCompanyTestCase,
	make_snippe_setting,
)
from frappe_snippe.www import snippe_checkout

GATEWAY = payment_gateway.get_gateway_name(TZS_COMPANY)


def make_client(session_status="pending"):
	return make_reply_client(
		create_session={
			"reference": "sess_pr",
			"status": "pending",
			"checkout_url": "https://checkout.snippe.sh/sess_pr",
		},
		get_session={"status": session_status, "amount": 20000, "currency": "TZS"},
	)


class PaymentGatewayTestCase(TzsCompanyTestCase):
	def setUp(self):
		frappe.db.savepoint("gateway_test")
		self.setting = make_snippe_setting()
		self.customer = make_customer().name

	def tearDown(self):
		frappe.db.rollback(save_point="gateway_test")

	def make_request(self, rate=20000):
		invoice = make_sales_invoice(self.customer, rate)
		return make_payment_request(
			dt="Sales Invoice",
			dn=invoice.name,
			payment_gateway_account=frappe.db.get_value(
				"Payment Gateway Account", {"payment_gateway": GATEWAY}
			),
			mute_email=1,
			submit_doc=1,
			return_doc=1,
		)

	def open_checkout(self, request, client=None):
		client = client or make_client()
		with (
			patch.object(payment_collection, "get_snippe_client", return_value=client),
			patch.object(payment_gateway, "get_snippe_client", return_value=client),
		):
			url = payment_gateway.get_checkout_url(request.name)
		return url, client

	def pay(self, request, paid_amount, fee_amount):
		self.open_checkout(request)
		collection = frappe.get_doc("Payment Collection", {"payment_request": request.name})
		with patch.object(webhooks, "notify_owner"):
			webhooks.book_collection(collection, paid_amount, fee_amount)
		return collection


class TestGatewaySetup(PaymentGatewayTestCase):
	def test_setting_creates_its_gateway_and_wallet_account(self):
		gateway = frappe.db.get_value("Payment Gateway", GATEWAY, ["gateway_settings", "gateway_controller"])
		account = frappe.db.get_value(
			"Payment Gateway Account",
			{"payment_gateway": GATEWAY},
			["payment_account", "currency", "company"],
		)
		self.assertEqual(gateway, ("Snippe Setting", TZS_COMPANY))
		self.assertEqual(account, (TZS_CASH, "TZS", TZS_COMPANY))

	def test_deleting_the_setting_removes_its_gateway(self):
		self.setting.delete()
		self.assertFalse(frappe.db.exists("Payment Gateway", GATEWAY))

	def test_only_tzs_is_accepted(self):
		with self.assertRaisesRegex(frappe.ValidationError, "TZS only"):
			self.setting.validate_transaction_currency("USD")
		self.setting.validate_transaction_currency("TZS")

	def test_payment_url_is_signed(self):
		url = self.setting.get_payment_url(reference_docname="ACC-PRQ-1", amount=1000)
		self.assertIn("/snippe_checkout?payment_request=ACC-PRQ-1&_signature=", url)


class TestCheckout(PaymentGatewayTestCase):
	def test_link_opens_one_snippe_checkout_for_the_request(self):
		request = self.make_request()
		url, client = self.open_checkout(request)
		again, _client = self.open_checkout(request, client)

		self.assertEqual((url, again), ("https://checkout.snippe.sh/sess_pr",) * 2)
		client.create_session.assert_called_once()
		collection = frappe.get_doc("Payment Collection", {"payment_request": request.name})
		self.assertEqual(
			(collection.customer, collection.total_amount, collection.status),
			(self.customer, 20000, "Pending"),
		)
		self.assertEqual(collection.owner, request.owner)

	def test_expired_checkout_is_replaced_with_a_new_one(self):
		request = self.make_request()
		self.open_checkout(request)
		with patch.object(webhooks, "notify_owner"):
			self.open_checkout(request, make_client(session_status="expired"))

		statuses = frappe.get_all(
			"Payment Collection", {"payment_request": request.name}, pluck="status", order_by="creation"
		)
		self.assertEqual(statuses, ["Expired", "Pending"])

	def test_request_with_cents_is_charged_whole_tzs(self):
		request = self.make_request(19999.5)
		self.open_checkout(request)
		collection = frappe.get_doc("Payment Collection", {"payment_request": request.name})
		self.assertEqual(collection.total_amount, 20000)

	def test_paid_request_link_is_refused(self):
		request = self.make_request()
		request.db_set("status", "Paid")
		with self.assertRaisesRegex(frappe.ValidationError, "not valid"):
			self.open_checkout(request)


class TestPaymentRequestBooking(PaymentGatewayTestCase):
	def test_paid_request_gets_a_payment_entry_with_the_fee_deducted(self):
		request = self.make_request(20000)
		self.pay(request, 20000, 400)

		request.reload()
		payment_entry = frappe.get_doc(
			"Payment Entry", {"reference_no": "sess_pr", "docstatus": 1, "party": self.customer}
		)
		self.assertEqual(request.status, "Paid")
		self.assertEqual(payment_entry.references[0].payment_request, request.name)
		self.assertEqual(
			(payment_entry.paid_to, payment_entry.paid_amount, payment_entry.deductions[0].account),
			(TZS_CASH, 19600, TZS_FEES),
		)
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", request.reference_name, "outstanding_amount"), 0
		)

	def test_amount_other_than_the_request_becomes_an_advance(self):
		request = self.make_request(20000)
		self.pay(request, 15000, 0)

		request.reload()
		self.assertNotEqual(request.status, "Paid")
		advance = frappe.get_all(
			"Payment Entry",
			filters={"party": self.customer, "docstatus": 1, "unallocated_amount": 15000},
			pluck="name",
		)
		self.assertEqual(len(advance), 1)


class TestCheckoutPage(PaymentGatewayTestCase):
	def test_page_redirects_to_snippe_and_keeps_the_collection(self):
		request = self.make_request()
		frappe.form_dict.payment_request = request.name
		try:
			with (
				patch.object(snippe_checkout, "verify_request", return_value=True),
				patch.object(payment_collection, "get_snippe_client", return_value=make_client()),
				self.assertRaises(frappe.Redirect),
			):
				snippe_checkout.get_context(frappe._dict())
			self.assertEqual(frappe.local.flags.redirect_location, "https://checkout.snippe.sh/sess_pr")
			self.assertTrue(frappe.local.flags.commit)
		finally:
			frappe.form_dict.pop("payment_request", None)
			frappe.local.flags.commit = False

	def test_unknown_request_is_refused(self):
		self.assert_refused("ACC-PRQ-MISSING", is_signed=True)

	def test_unsigned_link_is_refused(self):
		self.assert_refused(self.make_request().name, is_signed=False)

	def assert_refused(self, payment_request, is_signed):
		frappe.form_dict.payment_request = payment_request
		try:
			with (
				patch.object(snippe_checkout, "verify_request", return_value=is_signed),
				self.assertRaisesRegex(frappe.ValidationError, "not valid"),
			):
				snippe_checkout.get_context(frappe._dict())
		finally:
			frappe.form_dict.pop("payment_request", None)

	def test_collection_without_a_request_needs_a_mode_of_payment(self):
		collection = frappe.get_doc(
			{"doctype": "Payment Collection", "company": TZS_COMPANY, "customer": self.customer}
		)
		with self.assertRaisesRegex(frappe.MandatoryError, "Mode of Payment is required"):
			collection.before_validate()


class TestPaymentRequestEdgeCases(PaymentGatewayTestCase):
	def test_whole_tzs_charge_still_pays_a_request_with_cents(self):
		request = self.make_request(19999.5)
		self.pay(request, 20000, 0)

		request.reload()
		self.assertEqual(request.status, "Paid")
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", request.reference_name, "outstanding_amount"), 0
		)

	def test_retry_keeps_the_payment_request(self):
		request = self.make_request()
		self.open_checkout(request)
		collection = frappe.get_doc("Payment Collection", {"payment_request": request.name})
		collection.db_set("status", "Expired")

		with patch.object(payment_collection, "get_snippe_client", return_value=make_client()):
			retry = frappe.get_doc("Payment Collection", collection.retry())

		self.assertEqual((retry.payment_request, retry.status), (request.name, "Pending"))
