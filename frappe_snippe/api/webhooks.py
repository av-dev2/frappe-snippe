import json

import frappe
from frappe import _
from frappe.utils import flt, get_url
from snippe import WebhookHandler, WebhookVerificationError

from frappe_snippe.api.client import PAYMENT_STATUS, PAYOUT_STATUS

PAYMENT_EVENTS = {
	f"payment.{status}": PAYMENT_STATUS[status] for status in ("completed", "failed", "expired", "voided")
}
PAYOUT_EVENTS = {f"payout.{status}": PAYOUT_STATUS[status] for status in ("completed", "failed", "reversed")}


@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
def handle():
	"""Verify a Snippe webhook and queue it for processing."""
	settings = frappe.get_single("Snippe Settings")
	if not settings.enabled:
		return reject(403, "Integration disabled")

	secret = settings.get_password("webhook_secret", raise_exception=False)
	if not secret:
		return reject(500, "Webhook secret not configured")

	# verify_webhook() also parses the legacy flat payload, which rejects the 2026-01-25 envelope.
	body = frappe.request.get_data()
	try:
		WebhookHandler(secret).verify_signature(
			body,
			frappe.get_request_header("X-Webhook-Signature"),
			frappe.get_request_header("X-Webhook-Timestamp"),
		)
	except WebhookVerificationError as error:
		return reject(401, error.message)

	event_data = json.loads(body)
	if not isinstance(event_data, dict) or not (
		event_data.get("id") and event_data.get("type") and isinstance(event_data.get("data"), dict)
	):
		return reject(400, "Malformed webhook payload")

	if frappe.db.exists("Payment Log", {"event_id": event_data["id"]}):
		return {"status": "success", "message": "Already processed"}

	frappe.enqueue("frappe_snippe.api.webhooks.process_webhook", event_data=event_data, queue="short")
	return {"status": "success"}


def reject(status_code, message):
	frappe.local.response.http_status_code = status_code
	return {"status": "error", "message": message}


def process_webhook(event_data):
	event_type = event_data["type"]
	new_status = PAYMENT_EVENTS.get(event_type) or PAYOUT_EVENTS.get(event_type)
	if not new_status:
		frappe.log_error(title="Unhandled Snippe webhook event", message=json.dumps(event_data, indent=2))
		return

	is_payment = event_type in PAYMENT_EVENTS
	source_doctype = "Payment Collection" if is_payment else "Payment Disbursement"
	payload = event_data["data"]
	metadata = payload.get("metadata") or {}
	doc_name = metadata.get("erpnext_doc")
	if not doc_name and is_payment and payload.get("session_reference"):
		# Snippe's documented session event sends session_reference but no metadata.
		doc_name = frappe.db.get_value(
			"Payment Collection", {"snippe_reference": payload["session_reference"]}
		)

	frappe.get_doc(
		{
			"doctype": "Payment Log",
			"event_id": event_data["id"],
			"event_type": event_type,
			"direction": "Inbound" if is_payment else "Outbound",
			"source_doctype": source_doctype if doc_name else None,
			"source_name": doc_name,
			"snippe_reference": payload.get("reference"),
			"status": payload.get("status"),
			"amount": (payload.get("amount") or {}).get("value", 0),
			"channel": (payload.get("channel") or {}).get("provider"),
			"failure_reason": payload.get("failure_reason"),
			"raw_payload": json.dumps(event_data, indent=2),
		}
	).insert(ignore_permissions=True)

	if not doc_name:
		return
	if is_payment:
		process_inbound_webhook(doc_name, new_status, event_data["id"], payload)
	else:
		process_outbound_webhook(doc_name, new_status, payload, metadata)


def process_inbound_webhook(doc_name, new_status, event_id, payload):
	doc = frappe.get_doc("Payment Collection", doc_name, for_update=True)
	# A completed collection already has a Payment Entry; later events must not undo it.
	if doc.status in (new_status, "Completed"):
		return

	values = {"status": new_status, "snippe_event_id": event_id}
	if new_status == "Failed":
		values["failure_reason"] = payload.get("failure_reason")
	doc.db_set(values)

	if new_status != "Completed":
		for row in doc.references:
			row.db_set("status", "Failed")
		return

	paid_amount = (payload.get("amount") or {}).get("value")
	if not paid_amount:
		frappe.throw(_("Snippe event {0} for {1} has no paid amount.").format(event_id, doc_name))
	create_payment_entry(doc, paid_amount)
	if frappe.db.get_single_value("Snippe Settings", "auto_create_sales_invoice"):
		auto_create_sales_invoice(doc)


def process_outbound_webhook(doc_name, new_status, payload, metadata):
	row_id = metadata.get("row_id")
	if not row_id:
		return

	doc = frappe.get_doc("Payment Disbursement", doc_name)
	row = next((row for row in doc.items if row.name == row_id), None)
	if row:
		doc.set_item_status(row, new_status, payload.get("failure_reason"))

	doc.update_status()


def create_payment_entry(collection_doc, paid_amount):
	"""Post the amount Snippe received, allocated to references in row order."""
	settings = frappe.get_single("Snippe Settings")

	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = "Receive"
	pe.posting_date = collection_doc.posting_date
	pe.company = collection_doc.company
	pe.party_type = "Customer"
	pe.party = collection_doc.customer

	# Paid to is the Snippe account
	if settings.snippe_bank_account:
		pe.paid_to = settings.snippe_bank_account

	pe.paid_amount = paid_amount
	pe.received_amount = paid_amount
	pe.reference_no = collection_doc.snippe_reference
	pe.reference_date = collection_doc.posting_date

	allocations = collection_doc.get_reference_allocations(paid_amount)
	for row, amount in allocations:
		pe.append(
			"references",
			{
				"reference_doctype": row.reference_doctype,
				"reference_name": row.reference_name,
				"allocated_amount": amount,
			},
		)

	pe.insert(ignore_permissions=True)
	pe.submit()

	for row, amount in allocations:
		row.db_set(
			{"payment_entry": pe.name, "status": "Paid" if amount == flt(row.allocated_amount) else "Pending"}
		)


def auto_create_sales_invoice(collection_doc):
	for row in collection_doc.references:
		if row.reference_doctype == "Sales Order":
			# Check if invoice already exists
			if not frappe.db.exists("Sales Invoice", {"sales_order": row.reference_name}):
				so = frappe.get_doc("Sales Order", row.reference_name)
				from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

				si = make_sales_invoice(so.name)
				si.insert(ignore_permissions=True)
				si.submit()


@frappe.whitelist()
def get_webhook_url():
	"""Returns the webhook URL for Snippe dashboard configuration"""
	base_url = get_url()
	return f"{base_url}/api/method/frappe_snippe.api.webhooks.handle"
