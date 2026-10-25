import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

import frappe
from frappe.model.document import Document
from frappe.tests.utils import FrappeTestCase
from frappe.utils.password import remove_encrypted_password
from snippe import ValidationError, verify_webhook
from werkzeug.test import EnvironBuilder

from frappe_snippe import tasks
from frappe_snippe.api import webhooks
from frappe_snippe.tests.test_snippe_setting import (
	TZS_COMPANY,
	USD_COMPANY,
	TzsCompanyTestCase,
	make_payout_setting,
	make_snippe_setting,
)

SECRET = "whsec_test"


def make_event(event_type="payment.completed", event_id="evt_1", **data):
	return {
		"id": event_id,
		"type": event_type,
		"api_version": "2026-01-25",
		"created_at": "2026-01-24T10:30:00Z",
		"data": {
			"reference": "pi_1",
			"status": event_type.split(".")[1],
			"amount": {"value": 50000, "currency": "TZS"},
			"channel": {"type": "mobile_money", "provider": "airtel"},
			"metadata": {},
			**data,
		},
	}


def now():
	return str(int(time.time()))


def sign(body, timestamp, secret=SECRET):
	return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def set_args(db_set):
	return [call.args for call in db_set.call_args_list]


class TestWebhookVerification(TzsCompanyTestCase):
	def setUp(self):
		make_snippe_setting(webhook_secret=SECRET)
		frappe.local.response = frappe._dict()

	def post(self, event, timestamp=None, signature=None, headers=None):
		body = json.dumps(event).encode()
		timestamp = timestamp or now()
		if headers is None:
			headers = {
				"X-Webhook-Timestamp": timestamp,
				"X-Webhook-Signature": signature or sign(body, timestamp),
			}
		frappe.local.request = EnvironBuilder(method="POST", data=body, headers=headers).get_request()
		with patch.object(webhooks.frappe, "enqueue") as enqueue:
			result = webhooks.handle()
		return result, enqueue

	def test_valid_webhook_is_saved_before_it_is_queued(self):
		result, enqueue = self.post(make_event(event_id="evt_saved"))
		self.assertEqual(result, {"status": "success"})

		log = frappe.get_doc("Payment Log", {"event_id": "evt_saved"})
		self.assertEqual((log.processing_status, log.company), ("Queued", TZS_COMPANY))
		self.assertEqual(enqueue.call_args.kwargs["payment_log"], log.name)
		self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])

	def test_log_belongs_to_the_company_whose_account_signed(self):
		make_snippe_setting(webhook_secret="whsec_other")
		make_payout_setting(webhook_secret=SECRET)
		result, _enqueue = self.post(make_event(event_id="evt_wind"))

		self.assertEqual(result["status"], "success")
		self.assertEqual(frappe.db.get_value("Payment Log", {"event_id": "evt_wind"}, "company"), USD_COMPANY)

	def test_unknown_event_is_logged_but_not_saved(self):
		with patch.object(webhooks.frappe, "log_error") as log_error:
			result, enqueue = self.post(make_event("settlement.created", "evt_unknown"))
		self.assertEqual(result["status"], "success")
		log_error.assert_called_once()
		enqueue.assert_not_called()
		self.assertFalse(frappe.db.exists("Payment Log", {"event_id": "evt_unknown"}))

	def test_sha256_prefixed_signature_is_accepted(self):
		event = make_event()
		timestamp = now()
		result, _enqueue = self.post(
			event, timestamp, "sha256=" + sign(json.dumps(event).encode(), timestamp)
		)
		self.assertEqual(result["status"], "success")

	def test_invalid_signature_is_rejected(self):
		result, enqueue = self.post(make_event(), signature="0" * 64)
		self.assertEqual(frappe.local.response.http_status_code, 401)
		self.assertEqual(result["message"], "Invalid signature")
		enqueue.assert_not_called()

	def test_signature_must_cover_timestamp(self):
		body = json.dumps(make_event()).encode()
		_result, enqueue = self.post(
			make_event(), signature=hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
		)
		self.assertEqual(frappe.local.response.http_status_code, 401)
		enqueue.assert_not_called()

	def test_webhook_older_than_five_minutes_is_rejected(self):
		result, enqueue = self.post(make_event(), timestamp=str(int(time.time()) - 301))
		self.assertEqual(frappe.local.response.http_status_code, 401)
		self.assertEqual(result["message"], "Webhook timestamp expired")
		enqueue.assert_not_called()

	def test_missing_headers_are_rejected(self):
		result, _enqueue = self.post(make_event(), headers={})
		self.assertEqual(frappe.local.response.http_status_code, 401)
		self.assertEqual(result["message"], "Missing signature")

	def test_disabled_integration_and_missing_secret_are_rejected(self):
		remove_encrypted_password("Snippe Setting", TZS_COMPANY, "webhook_secret")
		result, _enqueue = self.post(make_event())
		self.assertEqual(
			(frappe.local.response.http_status_code, result["message"]),
			(401, "Webhook secret not configured"),
		)

		frappe.db.set_value("Snippe Setting", TZS_COMPANY, {"enable_collection": 0, "enable_payouts": 0})
		self.post(make_event())
		self.assertEqual(frappe.local.response.http_status_code, 403)

	def test_malformed_payload_is_rejected(self):
		_result, enqueue = self.post({"id": "evt_1", "type": "payment.completed"})
		self.assertEqual(frappe.local.response.http_status_code, 400)
		enqueue.assert_not_called()

	def test_non_object_payload_is_rejected(self):
		_result, enqueue = self.post(["payment.completed"])
		self.assertEqual(frappe.local.response.http_status_code, 400)
		enqueue.assert_not_called()

	def test_duplicate_event_is_not_queued(self):
		with patch.object(webhooks.frappe.db, "exists", return_value=True):
			result, enqueue = self.post(make_event())
		self.assertEqual(result["message"], "Already processed")
		enqueue.assert_not_called()

	def test_sdk_verify_webhook_rejects_envelope_payload(self):
		# Guards the reason handle() calls verify_signature() instead of verify_webhook().
		body = json.dumps(make_event()).encode()
		timestamp = now()
		with self.assertRaises(ValidationError):
			verify_webhook(body, sign(body, timestamp), timestamp, SECRET)


def insert_log(event, source_name=None):
	log = webhooks.make_payment_log(event, TZS_COMPANY)
	if source_name:
		# The collection itself is mocked, so its link is not checked.
		log.update({"source_doctype": "Payment Collection", "source_name": source_name})
		log.flags.ignore_links = True
	return log.insert(ignore_permissions=True)


class TestWebhookEvents(TzsCompanyTestCase):
	def test_payment_log_reads_envelope_fields(self):
		log = insert_log(make_event("payment.failed", "evt_log", failure_reason="Declined"))
		self.assertEqual(
			(
				log.event_type,
				log.direction,
				log.snippe_reference,
				log.amount,
				log.channel,
				log.failure_reason,
				log.processing_status,
			),
			("payment.failed", "Inbound", "pi_1", 50000, "airtel", "Declined", "Queued"),
		)

	def test_every_event_is_dispatched_with_its_status(self):
		expected = {
			"payment.completed": "Completed",
			"payment.failed": "Failed",
			"payment.expired": "Expired",
			"payment.voided": "Voided",
			"payout.completed": "Completed",
			"payout.failed": "Failed",
			"payout.reversed": "Reversed",
		}
		for event_type, status in expected.items():
			with patch.object(webhooks.frappe.db, "get_value", return_value="DOC-1"):
				log = webhooks.make_payment_log(
					make_event(event_type, metadata={"erpnext_doc": "DOC-1", "row_id": "row-1"}), TZS_COMPANY
				)
			with (
				patch.object(webhooks, "process_inbound_webhook") as inbound,
				patch.object(webhooks, "process_outbound_webhook") as outbound,
			):
				webhooks.apply_event(log)
			handler = inbound if event_type.startswith("payment.") else outbound
			self.assertEqual(handler.call_args.args[:2], ("DOC-1", status), event_type)

	def test_event_for_a_document_this_site_lacks_is_kept_without_link(self):
		log = insert_log(make_event(event_id="evt_foreign", metadata={"erpnext_doc": "PAY-COL-MISSING"}))
		self.assertEqual((log.source_doctype, log.source_name), (None, None))

	def test_failed_event_without_document_needs_no_processing(self):
		log = insert_log(make_event("payment.failed", event_id="evt_orphan"))
		with patch.object(webhooks, "process_inbound_webhook") as inbound:
			log.process()
		inbound.assert_not_called()
		self.assertEqual(log.processing_status, "Processed")


class TestPaymentLogProcessing(TzsCompanyTestCase):
	def insert_collection_log(self, event_id):
		return insert_log(make_event(event_id=event_id), source_name="PAY-COL-TEST")

	def test_processed_event_is_marked(self):
		log = self.insert_collection_log("evt_ok")
		with patch.object(webhooks, "process_inbound_webhook") as inbound:
			webhooks.process_payment_log(log.name)

		self.assertEqual(inbound.call_args.args, ("PAY-COL-TEST", "Completed", "evt_ok", log_data(log)))
		self.assertEqual(frappe.db.get_value("Payment Log", log.name, "processing_status"), "Processed")

	def test_failed_event_is_rolled_back_and_kept_for_reprocess(self):
		log = self.insert_collection_log("evt_fail")

		def fail_after_writing(*args):
			frappe.get_doc({"doctype": "ToDo", "description": "half-done snippe work"}).insert()
			frappe.throw("Accounting period is closed")

		with (
			patch.object(webhooks, "process_inbound_webhook", side_effect=fail_after_writing),
			patch.object(frappe, "log_error") as log_error,
		):
			log.process()

		self.assertFalse(frappe.db.exists("ToDo", {"description": "half-done snippe work"}))
		log.reload()
		self.assertEqual(log.processing_status, "Failed")
		self.assertIn("Accounting period is closed", log.processing_error)
		self.assertEqual(log_error.call_args.kwargs["reference_name"], log.name)

	def test_reprocess_applies_a_failed_event(self):
		log = self.insert_collection_log("evt_retry")
		log.db_set({"processing_status": "Failed", "processing_error": "boom"})
		with patch.object(webhooks, "process_inbound_webhook") as inbound:
			log.reprocess()

		inbound.assert_called_once()
		self.assertEqual((log.processing_status, log.processing_error), ("Processed", None))

	def test_processed_event_cannot_be_reprocessed(self):
		log = self.insert_collection_log("evt_done")
		log.db_set("processing_status", "Processed")
		with self.assertRaisesRegex(frappe.ValidationError, "already processed"):
			log.reprocess()

	def test_hourly_job_processes_logs_whose_job_was_lost(self):
		stale = insert_log(make_event("payment.failed", event_id="evt_stale"))
		stale.db_set("creation", frappe.utils.add_to_date(None, minutes=-30), update_modified=False)
		fresh = insert_log(make_event(event_id="evt_fresh"))
		tasks.process_stale_payment_logs()

		statuses = [
			frappe.db.get_value("Payment Log", log.name, "processing_status") for log in (stale, fresh)
		]
		self.assertEqual(statuses, ["Processed", "Queued"])


def log_data(log):
	return json.loads(log.raw_payload)["data"]


@patch.object(Document, "db_set")
class TestWebhookDocumentUpdates(FrappeTestCase):
	def make_collection(self, status="Pending"):
		return frappe.get_doc(
			{
				"doctype": "Payment Collection",
				"name": "PAY-COL-TEST",
				"status": status,
				"references": [{"reference_doctype": "Sales Invoice", "reference_name": "SINV-1"}],
			}
		)

	def run_inbound(self, collection, status, auto_invoice=0, amount=None, event_id="evt_9", **payload):
		payload = {
			"failure_reason": "Declined",
			"amount": amount or {"value": 50000, "currency": "TZS"},
			**payload,
		}
		with (
			patch.object(webhooks.frappe, "get_doc", return_value=collection),
			patch.object(
				webhooks,
				"get_snippe_setting",
				return_value=frappe._dict(auto_create_sales_invoice=auto_invoice),
			),
			patch.object(webhooks, "book_collection") as book_collection,
			patch.object(webhooks, "auto_create_sales_invoice") as auto_create_sales_invoice,
			patch.object(webhooks, "notify_owner") as self.notify_owner,
			patch.object(webhooks.frappe, "log_error") as self.log_error,
		):
			webhooks.process_inbound_webhook("PAY-COL-TEST", status, event_id, payload)
		return book_collection, auto_create_sales_invoice

	def test_completed_payment_posts_payment_entry(self, db_set):
		create_payment_entry, auto_create_sales_invoice = self.run_inbound(
			self.make_collection(), "Completed", 1
		)
		self.assertEqual(create_payment_entry.call_args.args[1:], (50000, 0))
		auto_create_sales_invoice.assert_called_once()
		self.assertIn(({"status": "Completed", "snippe_event_id": "evt_9"},), set_args(db_set))
		self.assertEqual(db_set.call_args_list[0].kwargs, {"notify": True})
		self.assertIn("50000 TZS received", self.notify_owner.call_args.args[1])

	def test_settlement_fee_reaches_the_payment_entry(self, db_set):
		create_payment_entry, _auto = self.run_inbound(
			self.make_collection(), "Completed", settlement={"fees": {"value": 1000, "currency": "TZS"}}
		)
		self.assertEqual(create_payment_entry.call_args.args[1:], (50000, 1000))
		self.log_error.assert_not_called()

	def test_payment_without_settlement_flags_the_unbooked_fee(self, db_set):
		create_payment_entry, _auto = self.run_inbound(self.make_collection(), "Completed")
		self.assertEqual(create_payment_entry.call_args.args[1:], (50000, 0))
		self.assertEqual(self.log_error.call_args.kwargs["title"], "Snippe fee not known")

	def test_status_check_sets_no_event_id(self, db_set):
		self.run_inbound(self.make_collection(), "Expired", event_id=None)
		self.assertIn(({"status": "Expired"},), set_args(db_set))

	def test_completed_event_without_amount_fails_loudly(self, db_set):
		with self.assertRaisesRegex(frappe.ValidationError, "no paid amount"):
			self.run_inbound(self.make_collection(), "Completed", amount={"currency": "TZS"})

	def test_repeated_completion_does_not_post_twice(self, db_set):
		create_payment_entry, _auto = self.run_inbound(self.make_collection("Completed"), "Completed")
		create_payment_entry.assert_not_called()
		db_set.assert_not_called()

	def test_event_after_completion_is_ignored(self, db_set):
		create_payment_entry, _auto = self.run_inbound(self.make_collection("Completed"), "Failed")
		create_payment_entry.assert_not_called()
		db_set.assert_not_called()

	def test_failed_payment_records_reason(self, db_set):
		create_payment_entry, _auto = self.run_inbound(self.make_collection(), "Failed")
		create_payment_entry.assert_not_called()
		self.assertIn(
			({"status": "Failed", "snippe_event_id": "evt_9", "failure_reason": "Declined"},),
			set_args(db_set),
		)
		self.assertIn(("status", "Failed"), set_args(db_set))
		self.assertIn("payment failed", self.notify_owner.call_args.args[1])

	def test_owner_gets_a_desk_notification(self, db_set):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "snippe owner"}).insert()
		webhooks.notify_owner(todo, "PAY-COL-1: 50000 TZS received")
		self.assertTrue(
			frappe.db.exists(
				"Notification Log",
				{
					"for_user": todo.owner,
					"document_name": todo.name,
					"subject": "PAY-COL-1: 50000 TZS received",
				},
			)
		)

	def test_payout_event_updates_matching_row(self, db_set):
		disbursement = frappe.get_doc(
			{
				"doctype": "Payment Disbursement",
				"items": [{"name": "row-1", "status": "Pending"}, {"name": "row-2", "status": "Pending"}],
			}
		)
		with patch.object(webhooks.frappe, "get_doc", return_value=disbursement):
			webhooks.process_outbound_webhook(
				"PAY-DIS-TEST",
				"Reversed",
				{"failure_reason": "Wallet closed", "metadata": {"row_id": "row-2"}},
			)
		self.assertIn(({"status": "Reversed", "failure_reason": "Wallet closed"},), set_args(db_set))
