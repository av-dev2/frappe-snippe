from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from snippe import NotFoundError, RateLimitError

from frappe_snippe import tasks
from frappe_snippe.tests.test_snippe_sdk import make_collection

PAYMENT = {
	"reference": "pi_1",
	"status": "completed",
	"amount": {"value": 1500, "currency": "TZS"},
	"settlement": {"fees": {"value": 30, "currency": "TZS"}},
}


def make_client(**replies):
	client = MagicMock()
	client.__enter__.return_value = client
	for method, reply in replies.items():
		getattr(client, method).return_value = reply
	return client


def make_session_collection(**values):
	collection = make_collection("Hosted Session")
	collection.update({"snippe_reference": "sess_1", **values})
	return collection


class TestSnippeUpdate(FrappeTestCase):
	def test_payment_status_comes_with_its_settlement(self):
		collection = make_collection()
		collection.snippe_reference = "pi_1"
		client = make_client(get_payment_data=PAYMENT)

		self.assertEqual(collection.get_snippe_update(client), ("Completed", PAYMENT))
		client.get_payment_data.assert_called_once_with("pi_1")

	def test_fixed_amount_session_reports_its_amount(self):
		client = make_client(get_session={"status": "completed", "amount": 1500, "currency": "TZS"})
		self.assertEqual(
			make_session_collection().get_snippe_update(client),
			("Completed", {"amount": {"value": 1500, "currency": "TZS"}}),
		)

	def test_custom_amount_session_amount_is_not_trusted(self):
		client = make_client(get_session={"status": "completed", "amount": 10000, "currency": "TZS"})
		status, payload = make_session_collection(allow_custom_amount=1).get_snippe_update(client)
		self.assertEqual((status, payload), ("Completed", {"amount": None}))

	def test_session_in_progress_stays_pending(self):
		client = make_client(get_session={"status": "active", "amount": 1500, "currency": "TZS"})
		self.assertEqual(make_session_collection().get_snippe_update(client)[0], "Pending")


class TestCollectionReconcile(FrappeTestCase):
	def make_doc(self, name, update=None, error=None):
		doc = MagicMock(company="Test Company")
		doc.name = name
		doc.get_snippe_update.return_value = update
		doc.get_snippe_update.side_effect = error
		return doc

	def run_reconcile(self, *docs, inbound_error=None):
		by_name = {doc.name: doc for doc in docs}
		with (
			patch.object(tasks.frappe, "get_all", return_value=list(by_name)) as get_all,
			patch.object(tasks.frappe, "get_doc", side_effect=lambda doctype, name: by_name[name]),
			patch.object(tasks, "get_snippe_client", return_value=make_client()),
			patch.object(tasks, "process_inbound_webhook", side_effect=inbound_error) as inbound,
			patch.object(tasks.frappe, "log_error") as log_error,
		):
			tasks.reconcile_pending_collections()
		return get_all, inbound, log_error

	def test_only_submitted_pending_collections_are_checked(self):
		get_all, _inbound, _log_error = self.run_reconcile()
		self.assertEqual(
			get_all.call_args.kwargs["filters"],
			{"docstatus": 1, "status": "Pending", "snippe_reference": ("is", "set")},
		)

	def test_final_status_is_applied_like_a_webhook(self):
		_get_all, inbound, _log_error = self.run_reconcile(
			self.make_doc("PAY-COL-1", ("Completed", PAYMENT)),
			self.make_doc("PAY-COL-2", ("Pending", {})),
		)
		inbound.assert_called_once_with("PAY-COL-1", "Completed", None, PAYMENT)

	def test_failed_check_is_logged_and_the_rest_still_run(self):
		_get_all, inbound, log_error = self.run_reconcile(
			self.make_doc("PAY-COL-1", error=NotFoundError("payment not found", 404)),
			self.make_doc("PAY-COL-2", ("Expired", {})),
		)
		inbound.assert_called_once_with("PAY-COL-2", "Expired", None, {})
		self.assertEqual(log_error.call_args.kwargs["reference_name"], "PAY-COL-1")

	def test_rate_limit_stops_the_run_until_the_next_hour(self):
		_get_all, inbound, log_error = self.run_reconcile(
			self.make_doc("PAY-COL-1", error=RateLimitError("Too many requests", 429)),
			self.make_doc("PAY-COL-2", ("Expired", {})),
		)
		inbound.assert_not_called()
		self.assertEqual(log_error.call_args.kwargs["title"], "Snippe rate limit reached")

	def test_failed_booking_is_rolled_back(self):
		def fail_after_writing(*args):
			frappe.get_doc({"doctype": "ToDo", "description": "half-booked collection"}).insert()
			frappe.throw("Accounting period is closed")

		_get_all, _inbound, log_error = self.run_reconcile(
			self.make_doc("PAY-COL-1", ("Completed", PAYMENT)), inbound_error=fail_after_writing
		)
		self.assertFalse(frappe.db.exists("ToDo", {"description": "half-booked collection"}))
		log_error.assert_called_once()
