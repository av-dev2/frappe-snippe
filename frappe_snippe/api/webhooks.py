import json
import re
from collections import defaultdict

import frappe
from erpnext.accounts.doctype.sales_invoice.sales_invoice import get_bank_cash_account
from frappe import _
from frappe.utils import flt, get_url, nowdate
from snippe import WebhookHandler, WebhookVerificationError

from frappe_snippe.accounting.wallet_entries import add_fee_deduction, post_wallet_entry
from frappe_snippe.api.client import PAYMENT_STATUS, PAYOUT_STATUS, get_snippe_setting
from frappe_snippe.api.payment_gateway import book_payment_request

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


def apply_event(payment_log) -> str | None:
	"""Move the log's Payment Collection or Payment Disbursement to the event's status.

	Returns "Needs Review" when a payment has no document and no customer could be found.
	"""
	payload = json.loads(payment_log.raw_payload)["data"]
	if not payment_log.source_name:
		if payment_log.event_type == "payment.completed":
			return book_unlinked_payment(payment_log, payload)
		return None

	new_status = EVENT_STATUS[payment_log.event_type]
	if payment_log.direction == "Inbound":
		process_inbound_webhook(payment_log.source_name, new_status, payment_log.event_id, payload)
	else:
		process_outbound_webhook(payment_log.source_name, new_status, payload)


def book_unlinked_payment(payment_log, payload) -> str | None:
	"""A payment with no ERPNext document, such as one through a payment link, becomes a customer advance."""
	customer = payment_log.customer or find_paying_customer(payload)
	if not customer:
		return "Needs Review"

	settlement = (payload.get("settlement") or {}).get("fees") or {}
	payment_entry = new_receive_payment_entry(
		company=payment_log.company,
		customer=customer,
		paid_amount=flt(payment_log.amount),
		fee_amount=flt(settlement.get("value")),
		reference_no=payment_log.snippe_reference,
		posting_date=nowdate(),
	)
	payment_entry.insert(ignore_permissions=True)
	payment_entry.submit()
	payment_log.db_set({"customer": customer, "payment_entry": payment_entry.name})
	return None


def find_paying_customer(payload) -> str | None:
	"""The Customer named in the payment link's ?meta=, else the only Customer with the payer's phone."""
	url_metadata = (payload.get("metadata") or {}).get("url_metadata") or {}
	if url_metadata.get("customer") and frappe.db.exists("Customer", url_metadata["customer"]):
		return url_metadata["customer"]

	# Phones are stored in many formats (0754 000 111, +255754000111), so compare the last 9 digits.
	digits = re.sub(r"\D", "", (payload.get("customer") or {}).get("phone") or "")[-9:]
	if len(digits) < 9:
		return None
	candidates = frappe.get_all(
		"Customer", filters={"mobile_no": ("like", "%" + "%".join(digits))}, fields=["name", "mobile_no"]
	)
	customers = [row.name for row in candidates if re.sub(r"\D", "", row.mobile_no).endswith(digits)]
	return customers[0] if len(customers) == 1 else None


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
	if collection.payment_request and book_payment_request(collection, paid_amount, fee_amount):
		return

	invoice = collection.get_draft_invoice()
	if collection.get_loan():
		add_loan_repayment(collection, paid_amount, fee_amount)
	elif invoice and invoice.is_pos:
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


def add_loan_repayment(collection, paid_amount, fee_amount):
	"""Repay the Loan into the Wallet Account; Snippe's fee leaves the wallet in its own entry."""
	setting = get_snippe_setting(collection.company)
	row = collection.references[0]
	repayment = frappe.get_doc(
		{
			"doctype": "Loan Repayment",
			"against_loan": row.reference_name,
			"applicant_type": "Customer",
			"applicant": collection.customer,
			"company": collection.company,
			"posting_date": collection.posting_date,
			"amount_paid": paid_amount,
			"payment_account": setting.get_account("wallet_account"),
			"reference_number": collection.snippe_reference,
			"reference_date": collection.posting_date,
		}
	)
	repayment.insert(ignore_permissions=True)
	repayment.submit()
	row.db_set("status", "Paid")

	if fee_amount:
		fee_entry = make_wallet_journal_entry(
			collection,
			{
				setting.get_account("fees_account"): fee_amount,
				setting.get_account("wallet_account"): -fee_amount,
			},
		)
		collection.db_set("wallet_journal_entry", fee_entry.name)


def add_pos_payment(invoice, collection, paid_amount, fee_amount):
	"""Fill the POS invoice's Phone payment row; the invoice books the payment when it is submitted."""
	row = find_payment_row(invoice, collection)
	row.update({"amount": paid_amount, "reference_no": collection.snippe_reference})
	invoice.save(ignore_permissions=True)

	if journal_entry := make_wallet_journal_entry(
		collection, get_pos_wallet_amounts(collection, paid_amount, fee_amount)
	):
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


def get_pos_wallet_amounts(collection, paid_amount, fee_amount) -> dict:
	"""Move a POS payment from its Mode of Payment account into the Wallet Account, less Snippe's fee.

	The submitted POS invoice debits the mode's account, which may be a sales team's own account;
	crediting it back keeps the Wallet Account equal to the Snippe balance.
	"""
	setting = get_snippe_setting(collection.company)
	amounts = defaultdict(float)
	amounts[get_bank_cash_account(collection.mode_of_payment, collection.company)["account"]] -= paid_amount
	amounts[setting.get_account("wallet_account")] += paid_amount - fee_amount
	if fee_amount:
		amounts[setting.get_account("fees_account")] += fee_amount
	return amounts


def make_wallet_journal_entry(collection, amounts: dict):
	"""Bank Entry for a collection's wallet movements; same-account amounts cancel out."""
	return post_wallet_entry(
		company=collection.company,
		posting_date=collection.posting_date,
		reference=collection.snippe_reference,
		remark=_("Snippe payment {0}").format(collection.name),
		amounts=amounts,
	)


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


def new_receive_payment_entry(
	*, company, customer, paid_amount, fee_amount, reference_no, posting_date, mode_of_payment=None
):
	"""Unsaved Payment Entry receiving into the Wallet Account, with Snippe's fee as a deduction."""
	setting = get_snippe_setting(company)
	pe = frappe.new_doc("Payment Entry")
	pe.update(
		{
			"payment_type": "Receive",
			"posting_date": posting_date,
			"company": company,
			"party_type": "Customer",
			"party": customer,
			"mode_of_payment": mode_of_payment,
			"paid_to": setting.get_account("wallet_account"),
			"paid_amount": paid_amount,
			"reference_no": reference_no,
			"reference_date": posting_date,
		}
	)
	add_fee_deduction(pe, setting.get_account("fees_account"), fee_amount)
	return pe


def create_payment_entry(collection_doc, paid_amount, fee_amount=0, allocate=True):
	"""Receive what the customer paid, allocated to references in row order, or as an advance."""
	pe = new_receive_payment_entry(
		company=collection_doc.company,
		customer=collection_doc.customer,
		paid_amount=paid_amount,
		fee_amount=fee_amount,
		reference_no=collection_doc.snippe_reference,
		posting_date=collection_doc.posting_date,
		mode_of_payment=collection_doc.mode_of_payment,
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
