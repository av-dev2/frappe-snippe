import frappe
import httpx
from frappe.model.document import Document
from snippe import AuthenticationError, ConflictError, RateLimitError, SnippeError, ValidationError

from frappe_snippe.api.client import (
	PAYOUT_STATUS,
	get_snippe_client,
	is_valid_idempotency_key,
	new_idempotency_key,
	normalize_phone,
	to_snippe_amount,
)
from frappe_snippe.api.webhooks import get_webhook_url


class PaymentDisbursement(Document):
	def before_insert(self):
		self.calculate_totals()

	def validate(self):
		for row in self.items:
			to_snippe_amount(row.amount)
			if not is_valid_idempotency_key(row.idempotency_key):
				row.idempotency_key = new_idempotency_key()
			if row.channel == "Mobile Money":
				row.phone_number = normalize_phone(row.phone_number)

	def calculate_totals(self):
		self.total_amount = sum(row.amount for row in self.items)

		# In a real scenario, we might want to calculate total_fee by calling an endpoint,
		# but the SDK might not expose fee estimation yet, so we'll leave it 0 or get it later.
		self.total_fee = 0
		self.total_to_deduct = self.total_amount + self.total_fee

	def on_submit(self):
		# We process this asynchronously because bulk disbursements can take time
		self.db_set("status", "Processing")
		frappe.enqueue(
			"frappe_snippe.frappe_snippe.doctype.payment_disbursement.payment_disbursement.process_disbursement_queue",
			payment_disbursement=self.name,
			queue="short",
		)


@frappe.whitelist()
def process_disbursement_queue(payment_disbursement: str):
	doc = frappe.get_doc("Payment Disbursement", payment_disbursement)
	client = get_snippe_client()

	settings = frappe.get_single("Snippe Settings")
	narration = doc.narration or settings.default_narration or "Payment"

	for row in doc.items:
		if row.status != "Pending" or row.snippe_reference:
			continue

		try:
			response = send_payout(client, row, narration, {"erpnext_doc": doc.name, "row_id": row.name})
		except (ValidationError, ConflictError) as error:
			row.db_set({"status": "Failed", "failure_reason": error.message})
			continue
		except (AuthenticationError, RateLimitError, SnippeError, httpx.TransportError) as error:
			# Stop without raising: a rollback would drop references of payouts already sent.
			frappe.log_error(
				title="Snippe payout batch stopped",
				message=str(error),
				reference_doctype="Payment Disbursement",
				reference_name=doc.name,
			)
			break

		row.db_set(
			{"snippe_reference": response.reference, "status": PAYOUT_STATUS.get(response.status, "Pending")}
		)

	doc.update_status()


def send_payout(client, row, narration, metadata):
	payout_kwargs = {
		"amount": to_snippe_amount(row.amount),
		"recipient_name": row.recipient_name,
		"narration": narration,
		"metadata": metadata,
		"idempotency_key": row.idempotency_key,
		"webhook_url": get_webhook_url(),
	}
	if row.channel == "Mobile Money":
		return client.create_mobile_payout(recipient_phone=normalize_phone(row.phone_number), **payout_kwargs)
	return client.create_bank_payout(
		recipient_bank=row.bank_code, recipient_account=row.bank_account, **payout_kwargs
	)


def update_status(self):
	total = len(self.items)
	completed = sum(1 for row in self.items if row.status == "Completed")
	failed = sum(1 for row in self.items if row.status in ("Failed", "Reversed"))

	if completed == total:
		self.db_set("status", "Completed")
	elif failed == total:
		self.db_set("status", "Failed")
	elif completed > 0 or failed > 0:
		self.db_set("status", "Partially Completed")
	else:
		self.db_set("status", "Processing")


PaymentDisbursement.update_status = update_status
