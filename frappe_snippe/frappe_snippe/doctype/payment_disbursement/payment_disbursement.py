import frappe
import httpx
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, fmt_money, format_datetime, now_datetime
from snippe import AuthenticationError, ConflictError, RateLimitError, SnippeError, ValidationError

from frappe_snippe.accounting.disbursement_entries import book_completed_payout, reverse_payout_booking
from frappe_snippe.api.client import (
	PAYOUT_STATUS,
	get_snippe_client,
	is_valid_idempotency_key,
	new_idempotency_key,
	normalize_phone,
	snippe_errors_as_messages,
	to_snippe_amount,
)
from frappe_snippe.api.webhooks import get_webhook_url


class PaymentDisbursement(Document):
	def validate(self):
		for row in self.items:
			to_snippe_amount(row.amount)
			if not is_valid_idempotency_key(row.idempotency_key):
				row.idempotency_key = new_idempotency_key()
			if row.channel == "Mobile Money":
				row.phone_number = normalize_phone(row.phone_number)
		if self.has_amounts_changed():
			self.clear_fees_and_balance()
		self.calculate_totals()

	def has_amounts_changed(self):
		before = self.get_doc_before_save()
		if not before:
			return False
		return {(row.name, flt(row.amount)) for row in before.items} != {
			(row.name, flt(row.amount)) for row in self.items
		}

	def clear_fees_and_balance(self):
		"""Fees and balance checked for other amounts no longer apply."""
		for row in self.items:
			row.fee_amount = 0
		self.snippe_balance = 0
		self.balance_checked_on = None

	def calculate_totals(self):
		self.total_amount = sum(row.amount for row in self.items)
		self.total_fee = sum(flt(row.fee_amount) for row in self.items)
		self.total_to_deduct = self.total_amount + self.total_fee

	def before_submit(self):
		if not self.balance_checked_on:
			frappe.throw(
				_("Click Check Fees & Balance before submitting."),
				title=_("Snippe Fees and Balance Not Checked"),
			)
		if flt(self.snippe_balance) < flt(self.total_to_deduct):
			frappe.throw(
				_(
					"Snippe balance {0} checked on {1} is below the {2} this disbursement needs, including fees."
				).format(
					fmt_money(self.snippe_balance, currency=self.get_currency()),
					format_datetime(self.balance_checked_on),
					fmt_money(self.total_to_deduct, currency=self.get_currency()),
				),
				title=_("Insufficient Snippe Balance"),
			)

	@frappe.whitelist()
	def check_fees_and_balance(self):
		"""Fetch Snippe fees and the wallet balance, save them on the draft, and return a preview."""
		self.check_permission("write")
		with get_snippe_client() as client, snippe_errors_as_messages():
			self.set_estimated_fees(client)
			balance = client.get_balance()
		self.snippe_balance = balance.available_balance
		self.balance_checked_on = now_datetime()
		self.save()
		return {
			"rows": [
				{
					"idx": row.idx,
					"recipient_name": row.recipient_name,
					"amount": row.amount,
					"fee_amount": row.fee_amount,
				}
				for row in self.items
			],
			"total_amount": self.total_amount,
			"total_fee": self.total_fee,
			"total_to_deduct": self.total_to_deduct,
			"available_balance": self.snippe_balance,
			"currency": balance.currency,
			"is_balance_enough": self.snippe_balance >= self.total_to_deduct,
		}

	def get_currency(self):
		return frappe.get_cached_value("Company", self.company, "default_currency")

	def set_estimated_fees(self, client):
		"""Fill each row's fee from Snippe; the payout reply replaces it with the charged fee."""
		fee_by_amount = {}
		for row in self.items:
			amount = to_snippe_amount(row.amount)
			if amount not in fee_by_amount:
				fee_by_amount[amount] = client.calculate_payout_fee(amount).fee_amount
			row.fee_amount = fee_by_amount[amount]
		self.calculate_totals()

	def set_item_status(self, row, status, failure_reason=None):
		"""Record a payout status change and book or reverse its accounting."""
		# The row lock stops the payout queue and a webhook booking the same change twice.
		if (
			row.status == status
			or frappe.db.get_value(row.doctype, row.name, "status", for_update=True) == status
		):
			return
		values = {"status": status}
		if status in ("Failed", "Reversed"):
			values["failure_reason"] = failure_reason or ""
		row.db_set(values)
		self.book_item(row)

	def book_item(self, row):
		"""Book or reverse a row's accounting for its current status."""
		# The payout already happened at Snippe, so an accounting failure must not roll back
		# the recorded status; it is logged and the hourly job retries completed rows.
		frappe.db.savepoint("snippe_payout_accounting")
		try:
			values = None
			if row.status == "Completed":
				values = book_completed_payout(self, row)
			elif row.status == "Reversed":
				values = reverse_payout_booking(self, row)
			if values:
				row.db_set(values)
		except Exception:
			frappe.db.rollback(save_point="snippe_payout_accounting")
			frappe.log_error(
				title="Snippe payout accounting failed",
				reference_doctype="Payment Disbursement",
				reference_name=self.name,
			)

	def on_submit(self):
		# We process this asynchronously because bulk disbursements can take time
		self.db_set("status", "Processing")
		frappe.enqueue(
			"frappe_snippe.frappe_snippe.doctype.payment_disbursement.payment_disbursement.process_disbursement_queue",
			payment_disbursement=self.name,
			queue="short",
		)


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
			row.db_set({"status": "Failed", "failure_reason": error.message, "fee_amount": 0})
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

		row.db_set({"snippe_reference": response.reference, "fee_amount": response.fees.value})
		doc.set_item_status(row, PAYOUT_STATUS.get(response.status, "Pending"))

	doc.calculate_totals()
	doc.db_set({"total_fee": doc.total_fee, "total_to_deduct": doc.total_to_deduct})
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
