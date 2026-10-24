import json
from unittest.mock import MagicMock, patch

import frappe
import httpx
from frappe.model.document import Document
from frappe.tests.utils import FrappeTestCase
from snippe import NotFoundError, RateLimitError, ValidationError

from frappe_snippe.api import webhooks
from frappe_snippe.api.client import SnippeClient
from frappe_snippe.frappe_snippe.doctype.payment_collection import payment_collection
from frappe_snippe.tests.test_collection_customer import make_collection, make_customer
from frappe_snippe.tests.test_snippe_setting import TZS_COMPANY, TzsCompanyTestCase, make_snippe_setting

BASE_URL = "https://api.snippe.test/api/v1"
SESSION = {
	"reference": "sess_abc123",
	"status": "pending",
	"amount": 1500,
	"checkout_url": "https://snippe.me/checkout/W0Sz",
	"payment_link_url": "https://snippe.me/p/Ax7k",
}


def make_client(status, body):
	"""A SnippeClient whose HTTP calls all get the given reply; returns it with the requests sent."""
	client = SnippeClient(api_key="snp_test", base_url=BASE_URL)
	requests = []

	def respond(request):
		requests.append(request)
		return httpx.Response(status, json=body)

	client._client = httpx.Client(
		base_url=BASE_URL, headers=client._client.headers, transport=httpx.MockTransport(respond)
	)
	return client, requests


class TestSessionClient(FrappeTestCase):
	def test_create_session_posts_only_given_fields(self):
		client, requests = make_client(201, {"code": 201, "data": SESSION})
		session = client.create_session(
			1500,
			line_items=[{"name": "Invoice", "quantity": 1, "unit_price": 1500}],
			redirect_url="https://shop.example/done",
			expires_in=600,
			idempotency_key="key-1",
		)

		self.assertEqual(session, SESSION)
		request = requests[0]
		self.assertEqual((request.method, request.url.path), ("POST", "/api/v1/sessions"))
		self.assertEqual(request.headers["Idempotency-Key"], "key-1")
		self.assertEqual(request.headers["Authorization"], "Bearer snp_test")
		self.assertEqual(
			json.loads(request.content),
			{
				"amount": 1500,
				"currency": "TZS",
				"redirect_url": "https://shop.example/done",
				"expires_in": 600,
				"line_items": [{"name": "Invoice", "quantity": 1, "unit_price": 1500}],
			},
		)

	def test_custom_amount_session_sends_bounds(self):
		client, requests = make_client(201, {"code": 201, "data": SESSION})
		client.create_session(allow_custom_amount=True, min_amount=1000, max_amount=500000)
		body = json.loads(requests[0].content)
		self.assertEqual(
			(body["allow_custom_amount"], body["min_amount"], body["max_amount"]), (True, 1000, 500000)
		)
		self.assertNotIn("amount", body)

	def test_snippe_validation_reply_raises_validation_error(self):
		client, _requests = make_client(400, {"message": "min_amount must be less than max_amount"})
		with self.assertRaisesRegex(ValidationError, "min_amount"):
			client.create_session(allow_custom_amount=True, min_amount=5000, max_amount=1000)

	def test_get_session_quotes_the_reference(self):
		client, requests = make_client(200, {"code": 200, "data": SESSION})
		self.assertEqual(client.get_session("sess/../x"), SESSION)
		self.assertEqual(requests[0].url.raw_path, b"/api/v1/sessions/sess%2F..%2Fx")

	def test_list_sessions_returns_sessions_and_meta(self):
		meta = {"total": 42, "limit": 10, "offset": 20}
		client, requests = make_client(200, {"code": 200, "data": [SESSION], "meta": meta})
		result = client.list_sessions(limit=10, offset=20, status="completed")
		self.assertEqual(result, {"sessions": [SESSION], **meta})
		self.assertEqual(dict(requests[0].url.params), {"limit": "10", "offset": "20", "status": "completed"})

	def test_cancel_session_posts_to_cancel(self):
		client, requests = make_client(200, {"code": 200, "message": "session cancelled"})
		client.cancel_session("sess_abc123")
		self.assertEqual(
			(requests[0].method, requests[0].url.path), ("POST", "/api/v1/sessions/sess_abc123/cancel")
		)

	def test_background_client_waits_for_the_rate_limit_reset_and_resends(self):
		replies = [
			httpx.Response(429, json={"message": "Too many requests"}, headers={"X-Ratelimit-Reset": "17"}),
			httpx.Response(429, json={"message": "Too many requests"}),
			httpx.Response(201, json={"data": SESSION}),
		]
		client = SnippeClient(api_key="snp_test", base_url=BASE_URL, rate_limit_waits=2)
		requests = []

		def respond(request):
			requests.append(request)
			return replies.pop(0)

		client._client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(respond))
		with patch("frappe_snippe.api.client.time.sleep") as sleep:
			self.assertEqual(client.create_session(1500, idempotency_key="key-1"), SESSION)

		self.assertEqual([call.args for call in sleep.call_args_list], [(17,), (60,)])
		self.assertEqual([request.headers["Idempotency-Key"] for request in requests], ["key-1"] * 3)

	def test_user_request_fails_at_once_on_the_rate_limit(self):
		client, requests = make_client(429, {"message": "Too many requests"})
		with self.assertRaises(RateLimitError):
			client.get_payout("po_1")
		self.assertEqual(len(requests), 1)

	def test_raw_payment_keeps_settlement(self):
		payment = {"reference": "pi_1", "status": "completed", "settlement": {"fees": {"value": 9}}}
		client, requests = make_client(200, {"code": 200, "data": payment})
		self.assertEqual(client.get_payment_data("pi_1"), payment)
		self.assertEqual(requests[0].url.path, "/api/v1/payments/pi_1")

	def test_http_errors_map_to_sdk_errors(self):
		client, _requests = make_client(404, {"message": "session not found"})
		with self.assertRaises(NotFoundError):
			client.get_session("sess_missing")


@patch.object(Document, "db_set")
class TestHostedSessionCollection(TzsCompanyTestCase):
	def setUp(self):
		self.customer = make_customer()
		self.client = MagicMock()
		self.client.__enter__.return_value = self.client
		self.client.create_session.return_value = SESSION

	def set_session_settings(self, expiry_minutes, redirect_url=None):
		make_snippe_setting(session_expiry_minutes=expiry_minutes, session_redirect_url=redirect_url)

	def make_session_collection(self, **values):
		collection = make_collection(self.customer, "Hosted Session")
		collection.update({"phone_number": None, "customer_name": self.customer.customer_name, **values})
		collection.append(
			"references",
			{"reference_doctype": "Sales Invoice", "reference_name": "SINV-0001", "allocated_amount": 1500.4},
		)
		return collection

	def run_with_client(self, method):
		with patch.object(payment_collection, "get_snippe_client", return_value=self.client):
			method()

	def test_hosted_session_is_created_and_stored(self, db_set):
		self.set_session_settings(30, "https://shop.example/thanks")
		self.client.create_session.return_value = {**SESSION, "status": "active"}
		self.run_with_client(self.make_session_collection().process_collection)

		args, kwargs = self.client.create_session.call_args
		self.assertEqual(args, (1500,))
		self.assertEqual(kwargs["expires_in"], 1800)
		self.assertEqual(kwargs["redirect_url"], "https://shop.example/thanks")
		self.assertEqual(kwargs["customer"], {"name": "Asha Juma Mrisho"})
		self.assertEqual(
			kwargs["line_items"],
			[{"id": "SINV-0001", "name": "Sales Invoice SINV-0001", "quantity": 1, "unit_price": 1500}],
		)
		self.assertEqual(kwargs["metadata"]["erpnext_doc"], "PAY-COL-TEST")
		values = db_set.call_args_list[0].args[0]
		self.assertEqual(values["snippe_reference"], "sess_abc123")
		self.assertEqual(values["status"], "Pending")
		self.assertEqual(values["payment_link_url"], "https://snippe.me/p/Ax7k")

	def test_empty_expiry_uses_snippe_default(self, db_set):
		self.set_session_settings(0)
		self.make_session_collection().create_session(self.client)
		self.assertIsNone(self.client.create_session.call_args.kwargs["expires_in"])

	def test_cancelling_pending_collection_cancels_session(self, db_set):
		pending = self.make_session_collection(status="Pending", snippe_reference="sess_abc123")
		completed = self.make_session_collection(status="Completed", snippe_reference="sess_done")
		self.run_with_client(pending.on_cancel)
		self.run_with_client(completed.on_cancel)

		self.client.cancel_session.assert_called_once_with("sess_abc123")
		db_set.assert_called_once_with("status", "Voided")

	def test_rejected_cancel_blocks_document_cancel(self, db_set):
		collection = self.make_session_collection(status="Pending", snippe_reference="sess_abc123")
		self.client.cancel_session.side_effect = ValidationError(
			"session already completed", 400, "invalid_state"
		)
		with self.assertRaisesRegex(frappe.ValidationError, "already completed"):
			self.run_with_client(collection.on_cancel)
		db_set.assert_not_called()


class TestSessionSettings(TzsCompanyTestCase):
	def test_expiry_outside_snippe_range_is_rejected(self):
		for minutes in (-5, 1441):
			with self.assertRaisesRegex(frappe.ValidationError, "Session Expiry"):
				make_snippe_setting(session_expiry_minutes=minutes)


class TestSessionWebhook(FrappeTestCase):
	def test_session_event_finds_collection_by_session_reference(self):
		event = {
			"id": "evt_sess",
			"type": "payment.completed",
			"data": {
				"reference": "pi_xyz789",
				"session_reference": "sess_abc123",
				"status": "completed",
				"amount": {"value": 1500, "currency": "TZS"},
			},
		}
		get_value = frappe.db.get_value

		def find_session(doctype, *args, **kwargs):
			if doctype == "Payment Collection":
				self.assertEqual(args, ({"company": TZS_COMPANY, "snippe_reference": "sess_abc123"},))
				return "PAY-COL-0001"
			return get_value(doctype, *args, **kwargs)

		with patch.object(webhooks.frappe.db, "get_value", side_effect=find_session):
			log = webhooks.make_payment_log(event, TZS_COMPANY)
		with patch.object(webhooks, "process_inbound_webhook") as inbound:
			webhooks.apply_event(log)

		self.assertEqual(log.source_name, "PAY-COL-0001")
		self.assertEqual(inbound.call_args.args[:2], ("PAY-COL-0001", "Completed"))
