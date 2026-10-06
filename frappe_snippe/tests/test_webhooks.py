import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

import frappe
from frappe.model.document import Document
from frappe.tests.utils import FrappeTestCase
from snippe import ValidationError, verify_webhook
from werkzeug.test import EnvironBuilder

from frappe_snippe.api import webhooks

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


class TestWebhookVerification(FrappeTestCase):
	def setUp(self):
		self.settings = MagicMock(enabled=1)
		self.settings.get_password.return_value = SECRET
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
		with (
			patch.object(webhooks.frappe, "get_single", return_value=self.settings),
			patch.object(webhooks.frappe, "enqueue") as enqueue,
		):
			result = webhooks.handle()
		return result, enqueue

	def test_valid_webhook_is_queued(self):
		event = make_event()
		result, enqueue = self.post(event)
		self.assertEqual(result, {"status": "success"})
		self.assertEqual(enqueue.call_args.kwargs["event_data"], event)

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
		self.settings.get_password.return_value = None
		self.post(make_event())
		self.assertEqual(frappe.local.response.http_status_code, 500)
		self.settings.enabled = 0
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


class TestWebhookEvents(FrappeTestCase):
	def test_payment_log_reads_envelope_fields(self):
		webhooks.process_webhook(make_event("payment.failed", "evt_log", failure_reason="Declined"))
		log = frappe.get_doc("Payment Log", {"event_id": "evt_log"})
		self.assertEqual(
			(
				log.event_type,
				log.direction,
				log.snippe_reference,
				log.amount,
				log.channel,
				log.failure_reason,
			),
			("payment.failed", "Inbound", "pi_1", 50000, "airtel", "Declined"),
		)

	def test_unknown_event_is_logged_without_processing(self):
		with (
			patch.object(webhooks, "process_inbound_webhook") as inbound,
			patch.object(webhooks, "process_outbound_webhook") as outbound,
			patch.object(webhooks.frappe, "log_error") as log_error,
		):
			webhooks.process_webhook(
				make_event("settlement.created", "evt_unknown", metadata={"erpnext_doc": "X"})
			)
		inbound.assert_not_called()
		outbound.assert_not_called()
		log_error.assert_called_once()
		self.assertFalse(frappe.db.exists("Payment Log", {"event_id": "evt_unknown"}))

	@patch.object(Document, "insert")
	def test_every_event_is_dispatched_with_its_status(self, insert):
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
			event = make_event(event_type, metadata={"erpnext_doc": "DOC-1", "row_id": "row-1"})
			with (
				patch.object(webhooks, "process_inbound_webhook") as inbound,
				patch.object(webhooks, "process_outbound_webhook") as outbound,
			):
				webhooks.process_webhook(event)
			handler = inbound if event_type.startswith("payment.") else outbound
			self.assertEqual(handler.call_args.args[:2], ("DOC-1", status), event_type)


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

	def run_inbound(self, collection, status, auto_invoice=0):
		with (
			patch.object(webhooks.frappe, "get_doc", return_value=collection),
			patch.object(webhooks.frappe.db, "get_single_value", return_value=auto_invoice),
			patch.object(webhooks, "create_payment_entry") as create_payment_entry,
			patch.object(webhooks, "auto_create_sales_invoice") as auto_create_sales_invoice,
		):
			webhooks.process_inbound_webhook("PAY-COL-TEST", status, "evt_9", {"failure_reason": "Declined"})
		return create_payment_entry, auto_create_sales_invoice

	def test_completed_payment_posts_payment_entry(self, db_set):
		create_payment_entry, auto_create_sales_invoice = self.run_inbound(
			self.make_collection(), "Completed", 1
		)
		create_payment_entry.assert_called_once()
		auto_create_sales_invoice.assert_called_once()
		self.assertIn(({"status": "Completed", "snippe_event_id": "evt_9"},), set_args(db_set))
		self.assertIn(("status", "Paid"), set_args(db_set))

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

	def test_payout_event_updates_matching_row(self, db_set):
		disbursement = frappe.get_doc(
			{
				"doctype": "Payment Disbursement",
				"items": [{"name": "row-1", "status": "Pending"}, {"name": "row-2", "status": "Pending"}],
			}
		)
		with patch.object(webhooks.frappe, "get_doc", return_value=disbursement):
			webhooks.process_outbound_webhook(
				"PAY-DIS-TEST", "Reversed", {"failure_reason": "Wallet closed"}, {"row_id": "row-2"}
			)
		self.assertIn(({"status": "Reversed", "failure_reason": "Wallet closed"},), set_args(db_set))
