import hashlib
import hmac
import json

import frappe
from frappe.utils import get_url


@frappe.whitelist(allow_guest=True)  # nosemgrep
def handle():
	"""
	Handle incoming webhooks from Snippe.
	"""
	settings = frappe.get_single("Snippe Settings")
	if not settings.enabled:
		frappe.local.response.http_status_code = 403
		return {"status": "error", "message": "Integration disabled"}

	# Get webhook payload and signature
	payload = frappe.request.get_data(as_text=True)
	signature = frappe.get_request_header("X-Snippe-Signature")

	if not signature:
		frappe.local.response.http_status_code = 401
		return {"status": "error", "message": "Missing signature"}

	# Verify HMAC signature
	secret = settings.get_password("webhook_secret")
	if not secret:
		frappe.local.response.http_status_code = 500
		return {"status": "error", "message": "Webhook secret not configured"}

	expected_sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()

	if not hmac.compare_digest(expected_sig, signature):
		frappe.local.response.http_status_code = 401
		return {"status": "error", "message": "Invalid signature"}

	# Parse payload
	data = json.loads(payload)
	event_id = data.get("id")

	# Check deduplication
	if frappe.db.exists("Payment Log", {"event_id": event_id}):
		return {"status": "success", "message": "Already processed"}

	# Enqueue background processing
	frappe.enqueue("frappe_snippe.api.webhooks.process_webhook", event_data=data, queue="short")

	return {"status": "success"}


def process_webhook(event_data):
	event_id = event_data.get("id")
	event_type = event_data.get("event")
	payload = event_data.get("data", {})

	reference = payload.get("id")
	status = payload.get("status")
	metadata = payload.get("metadata", {})

	erpnext_doc_name = metadata.get("erpnext_doc")

	direction = "Inbound" if event_type.startswith("payment") else "Outbound"

	# Create Payment Log
	log = frappe.get_doc(
		{
			"doctype": "Payment Log",
			"event_id": event_id,
			"event_type": event_type,
			"direction": direction,
			"snippe_reference": reference,
			"status": status,
			"amount": payload.get("amount", {}).get("value", 0),
			"channel": payload.get("channel"),
			"failure_reason": payload.get("failure_reason"),
			"raw_payload": json.dumps(event_data, indent=2),
		}
	)

	if erpnext_doc_name:
		log.source_doctype = "Payment Collection" if direction == "Inbound" else "Payment Disbursement"
		log.source_name = erpnext_doc_name

	log.insert(ignore_permissions=True)

	# Update documents
	if direction == "Inbound" and erpnext_doc_name:
		process_inbound_webhook(erpnext_doc_name, event_type, payload)
	elif direction == "Outbound" and erpnext_doc_name:
		process_outbound_webhook(erpnext_doc_name, event_type, payload, metadata)


def process_inbound_webhook(doc_name, event_type, payload):
	doc = frappe.get_doc("Payment Collection", doc_name)
	status_map = {
		"payment.successful": "Completed",
		"payment.failed": "Failed",
		"payment.expired": "Expired",
		"payment.voided": "Voided",
	}

	new_status = status_map.get(event_type)
	if new_status:
		doc.db_set("status", new_status)
		doc.db_set("snippe_event_id", payload.get("id"))

		if new_status == "Failed":
			doc.db_set("failure_reason", payload.get("failure_reason"))

		# Update references
		for row in doc.references:
			row.db_set("status", "Paid" if new_status == "Completed" else "Failed")

		# Create Payment Entry if completed
		if new_status == "Completed":
			create_payment_entry(doc)

			# Handle auto-create sales invoice
			settings = frappe.get_single("Snippe Settings")
			if settings.auto_create_sales_invoice:
				auto_create_sales_invoice(doc)


def process_outbound_webhook(doc_name, event_type, payload, metadata):
	doc = frappe.get_doc("Payment Disbursement", doc_name)
	row_id = metadata.get("row_id")

	status_map = {"payout.successful": "Completed", "payout.failed": "Failed", "payout.reversed": "Reversed"}

	new_status = status_map.get(event_type)
	if new_status and row_id:
		for row in doc.items:
			if row.name == row_id:
				row.db_set("status", new_status)
				if new_status in ("Failed", "Reversed"):
					row.db_set("failure_reason", payload.get("failure_reason"))
				break

		doc.update_status()


def create_payment_entry(collection_doc):
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

	pe.paid_amount = collection_doc.total_amount
	pe.received_amount = collection_doc.total_amount
	pe.reference_no = collection_doc.snippe_reference
	pe.reference_date = collection_doc.posting_date

	for row in collection_doc.references:
		pe.append(
			"references",
			{
				"reference_doctype": row.reference_doctype,
				"reference_name": row.reference_name,
				"allocated_amount": row.allocated_amount,
			},
		)

	pe.insert(ignore_permissions=True)
	pe.submit()

	# Link PE back to references
	for row in collection_doc.references:
		row.db_set("payment_entry", pe.name)


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
