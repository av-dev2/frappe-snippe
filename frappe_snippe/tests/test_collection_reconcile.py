from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from snippe import NotFoundError, RateLimitError

from frappe_snippe import tasks
from frappe_snippe.api.client import TooManyRecordsError
from frappe_snippe.tests.test_collection_customer import make_customer
from frappe_snippe.tests.test_paid_amount import make_paid_collection
from frappe_snippe.tests.test_snippe_sdk import make_collection
from frappe_snippe.tests.test_snippe_setting import (
	TZS_FEES,
	TzsCompanyTestCase,
	make_phone_mode_of_payment,
	make_snippe_setting,
)

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


def days_before_today(days):
	return frappe.utils.getdate(frappe.utils.add_days(frappe.utils.nowdate(), -days))


def make_session_collection(**values):
	collection = make_collection("Hosted Session")
	collection.update({"snippe_reference": "sess_1", **values})
	return collection


class TestSnippeUpdate(FrappeTestCase):
	def test_payment_status_is_read_by_reference(self):
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

	def test_completed_custom_amount_session_is_left_to_the_webhook(self):
		client = make_client(get_session={"status": "completed", "amount": 10000, "currency": "TZS"})
		status, payload = make_session_collection(allow_custom_amount=1).get_snippe_update(client)
		self.assertEqual((status, payload), ("Pending", {}))

	def test_expired_custom_amount_session_is_still_closed(self):
		client = make_client(get_session={"status": "expired", "amount": 10000, "currency": "TZS"})
		status, _payload = make_session_collection(allow_custom_amount=1).get_snippe_update(client)
		self.assertEqual(status, "Expired")

	def test_session_in_progress_stays_pending(self):
		client = make_client(get_session={"status": "active", "amount": 1500, "currency": "TZS"})
		self.assertEqual(make_session_collection().get_snippe_update(client)[0], "Pending")


class TestCollectionReconcile(FrappeTestCase):
	def make_doc(self, name, update=None, error=None, reference=None, days_ago=5):
		doc = MagicMock(company="Test Company", posting_date=days_before_today(days_ago))
		doc.name = name
		doc.snippe_reference = reference or f"pi_{name}"
		doc.get_snippe_update.return_value = update
		doc.get_snippe_update.side_effect = error
		return doc

	def run_reconcile(self, *docs, inbound_error=None, listed=None, list_error=None):
		by_name = {doc.name: doc for doc in docs}
		rows = [
			frappe._dict(name=doc.name, company=doc.company, posting_date=doc.posting_date) for doc in docs
		]
		client = make_client()
		with (
			patch.object(tasks.frappe, "get_all", return_value=rows) as get_all,
			patch.object(tasks.frappe, "get_doc", side_effect=lambda doctype, name: by_name[name]),
			patch.object(tasks, "get_snippe_client", return_value=client),
			patch.object(
				tasks, "list_since", return_value=listed or {}, side_effect=list_error
			) as list_since,
			patch.object(tasks, "process_inbound_webhook", side_effect=inbound_error) as inbound,
			patch.object(tasks.frappe, "log_error") as log_error,
		):
			tasks.reconcile_pending_collections()
		return get_all, inbound, log_error, list_since

	def test_only_submitted_pending_collections_are_checked(self):
		get_all, _inbound, _log_error, _list_since = self.run_reconcile()
		self.assertEqual(
			get_all.call_args.kwargs["filters"],
			{"docstatus": 1, "status": "Pending", "snippe_reference": ("is", "set")},
		)

	def test_listed_payment_is_booked_with_its_fee(self):
		doc = self.make_doc("PAY-COL-1", reference="pi_1")
		_get_all, inbound, _log_error, list_since = self.run_reconcile(doc, listed={"pi_1": PAYMENT})

		inbound.assert_called_once_with("PAY-COL-1", "Completed", None, PAYMENT)
		doc.get_snippe_update.assert_not_called()
		self.assertEqual(list_since.call_args.args[1], days_before_today(6))

	def test_list_reaches_back_a_week_at_most(self):
		_get_all, _inbound, _log_error, list_since = self.run_reconcile(
			self.make_doc("PAY-COL-1", ("Pending", {}), days_ago=90)
		)
		self.assertEqual(list_since.call_args.args[1], days_before_today(tasks.LIST_LOOKBACK_DAYS + 1))

	def test_unlisted_collection_falls_back_to_the_status_check(self):
		_get_all, inbound, _log_error, _list_since = self.run_reconcile(
			self.make_doc("PAY-COL-1", ("Completed", PAYMENT)),
			self.make_doc("PAY-COL-2", ("Pending", {})),
		)
		inbound.assert_called_once_with("PAY-COL-1", "Completed", None, PAYMENT)

	def test_unread_list_is_logged_and_status_checks_still_run(self):
		_get_all, inbound, log_error, _list_since = self.run_reconcile(
			self.make_doc("PAY-COL-1", ("Expired", {})),
			list_error=TooManyRecordsError("Snippe has more than 2000 records."),
		)
		self.assertEqual(log_error.call_args.kwargs["title"], "Snippe payment list not read")
		inbound.assert_called_once_with("PAY-COL-1", "Expired", None, {})

	def test_failed_check_is_logged_and_the_rest_still_run(self):
		_get_all, inbound, log_error, _list_since = self.run_reconcile(
			self.make_doc("PAY-COL-1", error=NotFoundError("payment not found", 404)),
			self.make_doc("PAY-COL-2", ("Expired", {})),
		)
		inbound.assert_called_once_with("PAY-COL-2", "Expired", None, {})
		self.assertEqual(log_error.call_args.kwargs["reference_name"], "PAY-COL-1")

	def test_rate_limit_stops_the_run_until_the_next_hour(self):
		_get_all, inbound, log_error, _list_since = self.run_reconcile(
			self.make_doc("PAY-COL-1", error=RateLimitError("Too many requests", 429)),
			self.make_doc("PAY-COL-2", ("Expired", {})),
		)
		inbound.assert_not_called()
		self.assertEqual(log_error.call_args.kwargs["title"], "Snippe rate limit reached")

	def test_failed_booking_is_rolled_back(self):
		def fail_after_writing(*args):
			frappe.get_doc({"doctype": "ToDo", "description": "half-booked collection"}).insert()
			frappe.throw("Accounting period is closed")

		_get_all, _inbound, log_error, _list_since = self.run_reconcile(
			self.make_doc("PAY-COL-1", ("Completed", PAYMENT)), inbound_error=fail_after_writing
		)
		self.assertFalse(frappe.db.exists("ToDo", {"description": "half-booked collection"}))
		log_error.assert_called_once()


class TestListedPaymentBooking(TzsCompanyTestCase):
	"""Posts a real Payment Entry for a collection whose webhook never arrived."""

	def setUp(self):
		make_snippe_setting()
		make_phone_mode_of_payment()

	def test_missed_webhook_collection_gets_its_snippe_fee(self):
		collection = make_paid_collection(make_customer().name, 20000)
		collection.db_set({"docstatus": 1, "status": "Pending", "snippe_reference": "pi_missed"})
		listed = {
			"pi_missed": {
				"reference": "pi_missed",
				"status": "completed",
				"amount": {"value": 20000, "currency": "TZS"},
				"settlement": {"fees": {"value": 400, "currency": "TZS"}},
			}
		}
		with (
			patch.object(tasks, "get_snippe_client", return_value=make_client()),
			patch.object(tasks, "list_since", return_value=listed),
		):
			tasks.reconcile_pending_collections()

		collection.reload()
		payment_entry = frappe.get_doc("Payment Entry", collection.references[0].payment_entry)
		self.assertEqual(collection.status, "Completed")
		self.assertEqual(
			(
				payment_entry.paid_amount,
				payment_entry.deductions[0].account,
				payment_entry.deductions[0].amount,
			),
			(19600, TZS_FEES, 400),
		)
