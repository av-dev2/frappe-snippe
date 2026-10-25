import json
from collections import defaultdict

import frappe
from erpnext import get_default_cost_center
from erpnext.accounts.doctype.sales_invoice.sales_invoice import get_bank_cash_account
from frappe import _
from frappe.utils import flt, get_url
from snippe import WebhookHandler, WebhookVerificationError

from frappe_snippe.api.client import PAYMENT_STATUS, PAYOUT_STATUS, get_snippe_setting

PAYMENT_EVENTS = {
	f"payment.{status}": PAYMENT_STATUS[status] for status in ("completed", "failed", "expired", "voided")
}
PAYOUT_EVENTS = {f"payout.{status}": PAYOUT_STATUS[status] for status in ("completed", "failed", "reversed")}
EVENT_STATUS = {**PAYMENT_EVENTS, **PAYOUT_EVENTS}


@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
def handle():
	"""Verify a Snippe webhook, keep it as a Payment Log and queue the log for processing."""
	companies = frappe.get_all(
		"Snippe Setting", or_filters={"enable_collection": 1, "enable_payouts": 1}, pluck="name"
	)
	if not companies:
		return reject(403, "Integration disabled")

	body = frappe.request.get_data()
	try:
		company = verify_signature(body, companies)
	except WebhookVerificationError as error:
		return reject(401, error.message)

	event_data = json.loads(body)
	if not isinstance(event_data, dict) or not (
		event_data.get("id") and event_data.get("type") and isinstance(event_data.get("data"), dict)
	):
		return reject(400, "Malformed webhook payload")

	if event_data["type"] not in EVENT_STATUS:
		frappe.log_error(title="Unhandled Snippe webhook event", message=json.dumps(event_data, indent=2))
		return {"status": "success"}

	if frappe.db.exists("Payment Log", {"event_id": event_data["id"]}):
		return {"status": "success", "message": "Already processed"}

	# The log is saved before Snippe gets its 2xx, so a failed processing job cannot lose the event.
	payment_log = make_payment_log(event_data, company).insert(ignore_permissions=True)
	frappe.enqueue(
		"frappe_snippe.api.webhooks.process_payment_log",
		payment_log=payment_log.name,
		queue="short",
		enqueue_after_commit=True,
	)
	return {"status": "success"}


def verify_signature(body: bytes, companies: list[str]) -> str:
	"""Return the company whose Snippe account signed the webhook."""
	signature = frappe.get_request_header("X-Webhook-Signature")
	timestamp = frappe.get_request_header("X-Webhook-Timestamp")
	last_error = WebhookVerificationError("Webhook secret not configured")
	for company in companies:
		secret = get_snippe_setting(company).get_password("webhook_secret", raise_exception=False)
		if not secret:
			continue

		# verify_webhook() also parses the legacy flat payload, which rejects the 2026-01-25 envelope.
		try:
			WebhookHandler(secret).verify_signature(body, signature, timestamp)
			return company
		except WebhookVerificationError as error:
			last_error = error

	raise last_error


def reject(status_code, message):
	frappe.local.response.http_status_code = status_code
	return {"status": "error", "message": message}


def make_payment_log(event_data, company):
	"""A Payment Log for an event from the company's Snippe account, linked to that company's document."""
	event_type = event_data["type"]
	is_payment = event_type in PAYMENT_EVENTS
	source_doctype = "Payment Collection" if is_payment else "Payment Disbursement"
	payload = event_data["data"]
	doc_name = None
	metadata = payload.get("metadata") or {}
	# Events for documents this company does not have stay unlinked; Snippe would otherwise retry them.
	if metadata.get("erpnext_doc"):
		doc_name = frappe.db.get_value(source_doctype, {"company": company, "name": metadata["erpnext_doc"]})
	elif is_payment and payload.get("session_reference"):
		# Snippe's documented session event sends session_reference but no metadata.
		doc_name = frappe.db.get_value(
			source_doctype, {"company": company, "snippe_reference": payload["session_reference"]}
		)

	return frappe.get_doc(
		{
			"doctype": "Payment Log",
			"company": company,
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
			"processing_status": "Queued",
		}
	)


def process_payment_log(payment_log: str):
	frappe.get_doc("Payment Log", payment_log).process()


def apply_event(payment_log):
	"""Move the log's Payment Collection or Payment Disbursement to the event's status."""
	if not payment_log.source_name:
		return

	payload = json.loads(payment_log.raw_payload)["data"]
	new_status = EVENT_STATUS[payment_log.event_type]
	if payment_log.direction == "Inbound":
		process_inbound_webhook(payment_log.source_name, new_status, payment_log.event_id, payload)
	else:
		process_outbound_webhook(payment_log.source_name, new_status, payload)


def process_inbound_webhook(doc_name, new_status, event_id, payload):
	"""Apply a payment status from a webhook, or from a status check when event_id is None."""
	doc = frappe.get_doc("Payment Collection", doc_name, for_update=True)
	# A completed collection already has a Payment Entry; later events must not undo it.
	if doc.status in (new_status, "Completed"):
		return

	values = {"status": new_status}
	if event_id:
		values["snippe_event_id"] = event_id
	if new_status == "Failed":
		values["failure_reason"] = payload.get("failure_reason")
	doc.db_set(values, notify=True)

	if new_status != "Completed":
		for row in doc.references:
			row.db_set("status", "Failed")
		notify_owner(doc, _("{0}: payment {1}").format(doc.name, _(new_status).lower()))
		return

	paid_amount = (payload.get("amount") or {}).get("value")
	if not paid_amount:
		frappe.throw(_("Snippe reported {0} as paid but sent no paid amount.").format(doc_name))

	settlement = payload.get("settlement")
	if not settlement:
		frappe.log_error(
			title="Snippe fee not known",
			message=f"Snippe sent no settlement for {doc_name}, so its Payment Entry has no fee. Book the fee by hand.",
			reference_doctype="Payment Collection",
			reference_name=doc_name,
		)
	book_collection(doc, paid_amount, flt(((settlement or {}).get("fees") or {}).get("value")))
	if get_snippe_setting(doc.company).auto_create_sales_invoice:
		auto_create_sales_invoice(doc)
	notify_owner(doc, _("{0}: {1} TZS received").format(doc.name, paid_amount))


def process_outbound_webhook(doc_name, new_status, payload):
	row_id = (payload.get("metadata") or {}).get("row_id")
	if not row_id:
		return

	doc = frappe.get_doc("Payment Disbursement", doc_name)
	row = next((row for row in doc.items if row.name == row_id), None)
	if row:
		doc.set_item_status(row, new_status, payload.get("failure_reason"))

	doc.update_status()


def book_collection(collection, paid_amount, fee_amount):
	"""Record a completed collection: a Payment Entry, or for a draft invoice a payment row or an advance."""
	invoice = collection.get_draft_invoice()
	if invoice and invoice.is_pos:
		add_pos_payment(invoice, collection, paid_amount, fee_amount)
	elif invoice:
		add_advance(invoice, create_payment_entry(collection, paid_amount, fee_amount, allocate=False))
	elif collection.has_open_references():
		create_payment_entry(collection, paid_amount, fee_amount)
	else:
		# The invoice was settled another way meanwhile, so the money stays with the customer.
		payment_entry = create_payment_entry(collection, paid_amount, fee_amount, allocate=False)
		notify_owner(
			collection,
			_("{0}: paid after the invoice was settled; booked as advance {1}").format(
				collection.name, payment_entry.name
			),
		)


def add_pos_payment(invoice, collection, paid_amount, fee_amount):
	"""Fill the POS invoice's Phone payment row; the invoice books the payment when it is submitted."""
	row = find_payment_row(invoice, collection)
	row.update({"amount": paid_amount, "reference_no": collection.snippe_reference})
	invoice.save(ignore_permissions=True)

	if journal_entry := make_wallet_journal_entry(collection, paid_amount, fee_amount):
		collection.db_set("wallet_journal_entry", journal_entry.name)
	for reference in collection.references:
		reference.db_set("status", "Paid")


def find_payment_row(invoice, collection):
	"""The row the cashier paid from, else an unpaid row of the same mode, else a new row."""
	for row in invoice.payments:
		if row.name == collection.payment_row:
			return row
	for row in invoice.payments:
		if row.mode_of_payment == collection.mode_of_payment and not row.reference_no:
			return row
	return invoice.append("payments", {"mode_of_payment": collection.mode_of_payment})


def add_advance(invoice, payment_entry):
	"""Attach the advance Payment Entry so the draft invoice uses it when it is submitted."""
	invoice.append(
		"advances",
		{
			"reference_type": "Payment Entry",
			"reference_name": payment_entry.name,
			"remarks": payment_entry.remarks,
			"advance_amount": payment_entry.unallocated_amount,
			"allocated_amount": payment_entry.unallocated_amount,
			"ref_exchange_rate": 1,
		},
	)
	invoice.save(ignore_permissions=True)


def make_wallet_journal_entry(collection, paid_amount, fee_amount):
	"""Move a POS payment from its Mode of Payment account into the Wallet Account, less Snippe's fee.

	The submitted POS invoice debits the mode's account, which may be a sales team's own account;
	this entry credits it back so the Wallet Account matches the Snippe balance.
	"""
	setting = get_snippe_setting(collection.company)
	amounts = defaultdict(float)
	amounts[get_bank_cash_account(collection.mode_of_payment, collection.company)["account"]] -= paid_amount
	amounts[setting.get_account("wallet_account")] += paid_amount - fee_amount
	if fee_amount:
		amounts[setting.get_account("fees_account")] += fee_amount

	# When the mode posts to the Wallet Account itself, only the fee is left to book.
	amounts = {account: amount for account, amount in amounts.items() if amount}
	if not amounts:
		return None

	journal_entry = frappe.new_doc("Journal Entry")
	journal_entry.update(
		{
			"voucher_type": "Bank Entry",
			"company": collection.company,
			"posting_date": collection.posting_date,
			"cheque_no": collection.snippe_reference,
			"cheque_date": collection.posting_date,
			"user_remark": _("Snippe payment {0}").format(collection.name),
		}
	)
	cost_center = get_default_cost_center(collection.company)
	for account, amount in amounts.items():
		journal_entry.append(
			"accounts",
			{
				"account": account,
				"cost_center": cost_center,
				"debit_in_account_currency": max(amount, 0),
				"credit_in_account_currency": max(-amount, 0),
			},
		)
	journal_entry.insert(ignore_permissions=True)
	journal_entry.submit()
	return journal_entry


def notify_owner(doc, subject: str):
	"""Tell the person who started the Snippe work how it ended."""
	frappe.get_doc(
		{
			"doctype": "Notification Log",
			"for_user": doc.owner,
			"type": "Alert",
			"subject": subject,
			"document_type": doc.doctype,
			"document_name": doc.name,
		}
	).insert(ignore_permissions=True)


def create_payment_entry(collection_doc, paid_amount, fee_amount=0, allocate=True):
	"""Receive what the customer paid, allocated to references in row order, or as an advance.

	Snippe keeps its fee, so the Snippe account receives the net amount and the fee is a deduction.
	"""
	setting = get_snippe_setting(collection_doc.company)

	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = "Receive"
	pe.posting_date = collection_doc.posting_date
	pe.company = collection_doc.company
	pe.party_type = "Customer"
	pe.party = collection_doc.customer
	pe.mode_of_payment = collection_doc.mode_of_payment

	pe.paid_to = setting.get_account("wallet_account")
	pe.paid_amount = flt(paid_amount) - flt(fee_amount)
	pe.received_amount = pe.paid_amount
	pe.reference_no = collection_doc.snippe_reference
	pe.reference_date = collection_doc.posting_date

	if fee_amount:
		pe.append(
			"deductions",
			{
				"account": setting.get_account("fees_account"),
				"cost_center": get_default_cost_center(collection_doc.company),
				"amount": fee_amount,
			},
		)

	allocations = collection_doc.get_reference_allocations(paid_amount) if allocate else []
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
	if not allocate:
		for row in collection_doc.references:
			row.db_set({"payment_entry": pe.name, "status": "Paid"})

	return pe


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
