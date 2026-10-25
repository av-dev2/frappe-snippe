import json
from unittest.mock import MagicMock, patch

import frappe

from frappe_snippe.api import webhooks
from frappe_snippe.frappe_snippe.doctype.payment_collection import payment_collection
from frappe_snippe.tests.test_collection_customer import make_customer
from frappe_snippe.tests.test_invoice_payment import make_client, make_draft_sales_invoice
from frappe_snippe.tests.test_snippe_setting import (
	PHONE_MODE,
	TZS_CASH,
	TZS_COMPANY,
	TZS_FEES,
	TzsCompanyTestCase,
	make_phone_mode_of_payment,
	make_snippe_setting,
)
from frappe_snippe.tests.test_webhooks import make_event


class CollectionFlowTestCase(TzsCompanyTestCase):
	def setUp(self):
		make_snippe_setting()
		make_phone_mode_of_payment()
		self.customer = make_customer().name

	def make_payment(self, doctype, name, amount, **kwargs):
		with patch.object(payment_collection, "get_snippe_client", return_value=make_client()):
			collection_name = payment_collection.make_payment(
				doctype, name, "0712345678", PHONE_MODE, amount, **kwargs
			)
		return frappe.get_doc("Payment Collection", collection_name)


class TestUnlinkedPayments(CollectionFlowTestCase):
	def insert_link_payment(self, event_id, phone="+255754000111", url_metadata=None):
		metadata = {"url_metadata": url_metadata} if url_metadata else {}
		event = make_event(
			event_id=event_id,
			customer={"phone": phone},
			metadata=metadata,
			settlement={"fees": {"value": 750, "currency": "TZS"}},
		)
		log = webhooks.make_payment_log(event, TZS_COMPANY)
		return log.insert(ignore_permissions=True)

	def assert_advance(self, log, customer, amount=50000, fee=750):
		log.reload()
		self.assertEqual((log.processing_status, log.customer), ("Processed", customer))
		payment_entry = frappe.get_doc("Payment Entry", log.payment_entry)
		self.assertEqual(
			(payment_entry.party, payment_entry.paid_to, payment_entry.unallocated_amount),
			(customer, TZS_CASH, amount),
		)
		self.assertEqual(payment_entry.deductions[0].amount, fee)

	def test_payment_link_meta_names_the_customer(self):
		log = self.insert_link_payment("evt_meta", phone=None, url_metadata={"customer": self.customer})
		log.process()
		self.assert_advance(log, self.customer)

	def test_payer_phone_finds_the_only_matching_customer(self):
		frappe.db.set_value("Customer", self.customer, "mobile_no", "0754 000 111")
		log = self.insert_link_payment("evt_phone")
		log.process()
		self.assert_advance(log, self.customer)

	def test_unknown_payer_waits_for_review_then_books(self):
		other = make_customer("Second Payer").name
		for customer in (self.customer, other):
			frappe.db.set_value("Customer", customer, "mobile_no", "0754000111")

		log = self.insert_link_payment("evt_review")
		log.process()
		self.assertEqual(log.processing_status, "Needs Review")
		self.assertFalse(log.payment_entry)

		log.db_set("customer", other)
		log.reprocess()
		self.assert_advance(log, other)


class TestCustomerDeposit(CollectionFlowTestCase):
	def test_deposit_has_no_reference_and_books_an_advance(self):
		collection = self.make_payment("Customer", self.customer, 30000, company=TZS_COMPANY)
		self.assertEqual(
			(collection.references, collection.total_amount, collection.status), ([], 30000, "Pending")
		)

		webhooks.book_collection(collection, 30000, 300)
		payment_entry = frappe.get_all(
			"Payment Entry",
			filters={"party": self.customer, "docstatus": 1},
			fields=["unallocated_amount", "paid_to"],
		)
		self.assertEqual(payment_entry, [{"unallocated_amount": 30000, "paid_to": TZS_CASH}])

	def test_deposit_needs_a_positive_amount(self):
		with self.assertRaisesRegex(frappe.ValidationError, "more than 0"):
			self.make_payment("Customer", self.customer, 0, company=TZS_COMPANY)


class TestLoanRepayment(CollectionFlowTestCase):
	def test_amount_left_is_what_the_loan_still_needs(self):
		loan = frappe._dict(doctype="Loan", total_payment=120000, total_amount_paid=45000)
		self.assertEqual(payment_collection.get_amount_left(loan), 75000)

	def test_loan_must_belong_to_the_paying_customer(self):
		collection = frappe.get_doc(
			{
				"doctype": "Payment Collection",
				"customer": self.customer,
				"references": [{"reference_doctype": "Loan", "reference_name": "LOAN-1", "idx": 1}],
			}
		)
		with (
			patch.object(frappe.db, "get_value", return_value=("Customer", "Someone Else")),
			self.assertRaisesRegex(frappe.ValidationError, "is not a loan of Customer"),
		):
			collection.validate_loan()

	def test_paid_loan_collection_creates_a_repayment_into_the_wallet(self):
		collection = MagicMock(
			company=TZS_COMPANY,
			customer=self.customer,
			posting_date="2026-10-25",
			snippe_reference="pi_loan",
			references=[MagicMock(reference_name="LOAN-1")],
		)
		repayment = MagicMock()
		setting = webhooks.get_snippe_setting(TZS_COMPANY)
		with (
			patch.object(webhooks, "get_snippe_setting", return_value=setting),
			patch.object(webhooks.frappe, "get_doc", return_value=repayment) as get_doc,
			patch.object(
				webhooks, "make_wallet_journal_entry", return_value=frappe._dict(name="JV-FEE")
			) as fee,
		):
			webhooks.add_loan_repayment(collection, 40000, 500)

		values = get_doc.call_args.args[0]
		self.assertEqual(
			(values["doctype"], values["against_loan"], values["amount_paid"], values["payment_account"]),
			("Loan Repayment", "LOAN-1", 40000, TZS_CASH),
		)
		repayment.submit.assert_called_once()
		self.assertEqual(fee.call_args.args[1], {TZS_FEES: 500, TZS_CASH: -500})
		collection.db_set.assert_called_once_with("wallet_journal_entry", "JV-FEE")


class TestRetryAndResend(CollectionFlowTestCase):
	def test_pending_mobile_collection_resends_its_push(self):
		collection = self.make_payment(
			"Sales Invoice", make_draft_sales_invoice(self.customer, 9000).name, 9000
		)
		client = make_client()
		with patch.object(payment_collection, "get_snippe_client", return_value=client):
			collection.resend_push()
		client.trigger_push.assert_called_once_with("pi_push")

	def test_finished_collection_cannot_resend(self):
		collection = self.make_payment(
			"Sales Invoice", make_draft_sales_invoice(self.customer, 9000).name, 9000
		)
		collection.db_set("status", "Completed")
		with self.assertRaisesRegex(frappe.ValidationError, "Only a pending Mobile Money"):
			collection.resend_push()

	def test_failed_collection_is_retried_as_a_new_request(self):
		invoice = make_draft_sales_invoice(self.customer, 9000)
		failed = self.make_payment("Sales Invoice", invoice.name, 9000)
		failed.db_set({"status": "Failed", "failure_reason": "Declined"})

		with patch.object(payment_collection, "get_snippe_client", return_value=make_client()):
			retry = frappe.get_doc("Payment Collection", failed.retry())

		self.assertEqual((retry.retry_of, retry.status, retry.docstatus), (failed.name, "Pending", 1))
		self.assertNotEqual(retry.idempotency_key, failed.idempotency_key)
		self.assertEqual((retry.references[0].reference_name, retry.failure_reason), (invoice.name, None))

	def test_pending_collection_cannot_be_retried(self):
		collection = self.make_payment(
			"Sales Invoice", make_draft_sales_invoice(self.customer, 9000).name, 9000
		)
		with self.assertRaisesRegex(frappe.ValidationError, "Only a failed, expired or voided"):
			collection.retry()


class TestPushClient(TzsCompanyTestCase):
	def test_push_posts_to_the_payment(self):
		from frappe_snippe.tests.test_payment_sessions import make_client as make_http_client

		client, requests = make_http_client(200, {"code": 200, "data": {"reference": "pi_1"}})
		client.trigger_push("pi_1")
		self.assertEqual((requests[0].method, requests[0].url.path), ("POST", "/api/v1/payments/pi_1/push"))
		self.assertEqual(json.loads(requests[0].content or b"{}"), {})
