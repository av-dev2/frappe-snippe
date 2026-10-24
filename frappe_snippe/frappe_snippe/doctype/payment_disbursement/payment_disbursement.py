from collections import defaultdict

import frappe
import httpx
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, fmt_money, format_datetime, now_datetime, time_diff_in_hours
from snippe import AuthenticationError, ConflictError, RateLimitError, SnippeError, ValidationError

from frappe_snippe.accounting.amounts_due import get_amounts_left_to_pay
from frappe_snippe.accounting.disbursement_entries import book_completed_payout, reverse_payout_booking
from frappe_snippe.api.client import (
	BACKGROUND_RATE_LIMIT_WAITS,
	PAYOUT_STATUS,
	get_snippe_client,
	get_snippe_setting,
	is_valid_idempotency_key,
	new_idempotency_key,
	normalize_phone,
	snippe_errors_as_messages,
	to_snippe_amount,
	validate_rows_currency,
	validate_snippe_account_currency,
)
from frappe_snippe.api.webhooks import get_webhook_url

# Snippe keeps an idempotency key for 24 hours; after that a resent payout could pay twice.
RESUME_WINDOW_HOURS = 20


class PaymentDisbursement(Document):
	def validate(self):
		# validate runs before the mandatory check, so empty links are left to it.
		if self.company:
			get_snippe_setting(self.company).validate_payouts_enabled()
		if self.paid_from_account:
			validate_snippe_account_currency(self.paid_from_account)
		validate_rows_currency(self.items)
		for row in self.items:
			to_snippe_amount(row.amount)
			if not is_valid_idempotency_key(row.idempotency_key):
				row.idempotency_key = new_idempotency_key()
			if row.channel == "Mobile Money":
				row.phone_number = normalize_phone(row.phone_number)
			else:
				row.set_party_bank_details()
				row.validate_bank_details()
		self.validate_amounts_due()
		if self.has_amounts_changed():
			self.clear_fees_and_balance()
		self.calculate_totals()

	def validate_amounts_due(self):
		"""Stop any document being paid more than it still owes, across all disbursements."""
		requested = defaultdict(lambda: defaultdict(float))
		for row in self.items:
			requested[row.reference_doctype][row.reference_name] += flt(row.amount)

		precision = self.precision("total_amount")
		for reference_doctype, amounts in requested.items():
			left_to_pay = get_amounts_left_to_pay(reference_doctype, list(amounts), self.name)
			for reference_name, amount in amounts.items():
				left = flt(left_to_pay.get(reference_name, 0), precision)
				if flt(amount, precision) > left:
					frappe.throw(
						_("{0} {1} has {2} left to pay, but this disbursement pays {3}.").format(
							_(reference_doctype),
							reference_name,
							fmt_money(max(left, 0), currency=self.get_currency()),
							fmt_money(amount, currency=self.get_currency()),
						),
						title=_("Amount Exceeds What Is Owed"),
					)

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
		with get_snippe_client(self.company) as client, snippe_errors_as_messages():
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
		self.db_set({"status": "Processing", "queued_on": now_datetime()})
		self.queue_payouts()

	def queue_payouts(self):
		frappe.enqueue(
			"frappe_snippe.frappe_snippe.doctype.payment_disbursement.payment_disbursement.process_disbursement_queue",
			payment_disbursement=self.name,
			queue="long",
			job_id=f"snippe_payouts::{self.name}",
			deduplicate=True,
			enqueue_after_commit=True,
		)

	def get_unsent_rows(self):
		return [row for row in self.items if row.status == "Pending" and not row.snippe_reference]

	def resume_unsent_payouts(self):
		"""Resend rows a stopped batch left unsent, or fail them once a resend could pay twice."""
		if time_diff_in_hours(now_datetime(), self.queued_on) < RESUME_WINDOW_HOURS:
			# A batch still queued or running is not queued again; see queue_payouts.
			self.queue_payouts()
			return

		for row in self.get_unsent_rows():
			row.db_set(
				{
					"status": "Failed",
					"failure_reason": _(
						"Snippe did not confirm this payout within {0} hours. Check the Snippe dashboard before you pay it again."
					).format(RESUME_WINDOW_HOURS),
				}
			)
		self.update_status()


def process_disbursement_queue(payment_disbursement: str):
	doc = frappe.get_doc("Payment Disbursement", payment_disbursement)
	narration = doc.narration or get_snippe_setting(doc.company).default_narration or "Payment"

	with get_snippe_client(doc.company, BACKGROUND_RATE_LIMIT_WAITS) as client:
		for row in doc.get_unsent_rows():
			try:
				response = send_payout(client, row, narration, {"erpnext_doc": doc.name, "row_id": row.name})
			except (ValidationError, ConflictError) as error:
				row.db_set({"status": "Failed", "failure_reason": error.message, "fee_amount": 0})
				frappe.db.commit()  # nosemgrep
				continue
			except (AuthenticationError, RateLimitError, SnippeError, httpx.TransportError) as error:
				# Stop without raising; the hourly job resumes the unsent rows.
				frappe.log_error(
					title="Snippe payout batch stopped",
					message=str(error),
					reference_doctype="Payment Disbursement",
					reference_name=doc.name,
				)
				break

			row.db_set({"snippe_reference": response.reference, "fee_amount": response.fees.value})
			doc.set_item_status(row, PAYOUT_STATUS.get(response.status, "Pending"))
			# A sent payout is kept even if the job is killed later, so it is never sent again.
			frappe.db.commit()  # nosemgrep

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
