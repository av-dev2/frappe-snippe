import dataclasses
from collections import defaultdict

import frappe
from erpnext.accounts.utils import get_balance_on
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, get_url
from frappe.utils.verified_command import get_signed_params

from frappe_snippe.accounting.wallet_entries import post_wallet_entry
from frappe_snippe.api.client import (
	SNIPPE_CURRENCY,
	get_snippe_client,
	snippe_errors_as_messages,
	to_snippe_amount,
	validate_snippe_account_currency,
)
from frappe_snippe.api.payment_gateway import remove_payment_gateway, setup_payment_gateway


class SnippeSetting(Document):
	"""One company's Snippe account; a Snippe account serves one company only."""

	def validate(self):
		if self.session_expiry_minutes and not 1 <= cint(self.session_expiry_minutes) <= 1440:
			frappe.throw(_("Session Expiry must be between 1 and 1440 minutes."))

		self.validate_required_fields()
		self.validate_wallet_account()
		self.validate_fees_account()

	def validate_required_fields(self):
		# The form marks these fields mandatory too, but Frappe checks that only in the browser.
		required = []
		if self.enable_collection or self.enable_payouts:
			required += ["api_key", "webhook_secret", "wallet_account", "fees_account"]

		missing = [_(self.meta.get_label(fieldname)) for fieldname in required if not self.get(fieldname)]
		if missing:
			frappe.throw(
				_("Set {0} to use the enabled Snippe features.").format(", ".join(missing)),
				title=_("Missing Snippe Details"),
			)

	def validate_wallet_account(self):
		"""The Wallet Account mirrors the Snippe balance, so it is a TZS Bank or Cash ledger."""
		if not self.wallet_account:
			return

		self.validate_ledger_account("wallet_account")
		validate_snippe_account_currency(self.wallet_account)
		if frappe.get_cached_value("Account", self.wallet_account, "account_type") not in ("Bank", "Cash"):
			frappe.throw(_("Wallet Account must be a Bank or Cash account."))

	def validate_fees_account(self):
		if not self.fees_account:
			return

		self.validate_ledger_account("fees_account")
		if frappe.get_cached_value("Account", self.fees_account, "root_type") != "Expense":
			frappe.throw(_("Fees Account must be an Expense account."))

	def validate_ledger_account(self, fieldname: str):
		account = self.get(fieldname)
		company, is_group = frappe.get_cached_value("Account", account, ["company", "is_group"])
		if company != self.company or is_group:
			frappe.throw(
				_("{0} {1} must be a ledger account of Company {2}.").format(
					_(self.meta.get_label(fieldname)), account, self.company
				)
			)

	def validate_collection_enabled(self):
		if not self.enable_collection:
			frappe.throw(
				_("Payment collection through Snippe is disabled for Company {0}.").format(self.company),
				title=_("Snippe Collection Disabled"),
			)

	def validate_payouts_enabled(self):
		if not self.enable_payouts:
			frappe.throw(
				_("Payouts through Snippe are disabled for Company {0}.").format(self.company),
				title=_("Snippe Payouts Disabled"),
			)

	def get_account(self, fieldname: str) -> str:
		account = self.get(fieldname)
		if not account:
			frappe.throw(
				_("Set the {0} in Snippe Setting {1}.").format(_(self.meta.get_label(fieldname)), self.name)
			)
		return account

	def on_update(self):
		setup_payment_gateway(self)

	def on_trash(self):
		remove_payment_gateway(self.company)

	def validate_transaction_currency(self, currency: str):
		"""Payment Gateway controller check; Snippe collects in TZS only."""
		if currency != SNIPPE_CURRENCY:
			frappe.throw(_("Snippe collects payments in {0} only.").format(SNIPPE_CURRENCY))

	def validate_minimum_transaction_amount(self, currency: str, amount: float):
		to_snippe_amount(amount)

	def get_payment_url(self, **kwargs) -> str:
		"""Signed Payment Gateway link; the checkout opens only when the payer follows it."""
		return get_url(
			"/snippe_checkout?" + get_signed_params({"payment_request": kwargs["reference_docname"]})
		)

	@frappe.whitelist()
	def get_balance(self) -> dict:
		"""Live Snippe balance beside the Wallet Account balance, for the form; nothing is stored.

		Pending payouts are already taken from Snippe's available balance but are booked only once
		Snippe completes them, so they show as a difference until then.
		"""
		with get_snippe_client(self.company) as client, snippe_errors_as_messages():
			balance = dataclasses.asdict(client.get_balance())

		balance["wallet_balance"] = flt(
			get_balance_on(self.get_account("wallet_account"), company=self.company)
		)
		balance["difference"] = flt(balance["available_balance"]) - balance["wallet_balance"]
		return balance

	@frappe.whitelist()
	def record_wallet_transfer(
		self,
		transfer_type: str,
		bank_account: str,
		amount: float,
		posting_date: str,
		reference: str,
		fee: float = 0,
	) -> str:
		"""Book money moved between a bank account and the Snippe balance, so the Wallet Account follows."""
		self.check_permission("write")
		if transfer_type not in ("Top-up", "Withdrawal"):
			frappe.throw(_("Transfer Type must be Top-up or Withdrawal."))
		if flt(amount) <= 0 or flt(fee) < 0:
			frappe.throw(_("Amount must be more than 0 and the fee cannot be negative."))
		if not reference or frappe.db.exists(
			"Journal Entry",
			{"company": self.company, "voucher_type": "Contra Entry", "cheque_no": reference, "docstatus": 1},
		):
			frappe.throw(_("Enter the bank reference of this transfer. Each reference is recorded once."))

		wallet_account = self.get_account("wallet_account")
		if bank_account == wallet_account:
			frappe.throw(_("Choose the bank account the money moved to or from, not the Wallet Account."))
		self.validate_bank_account(bank_account)

		sign = 1 if transfer_type == "Top-up" else -1
		amounts = defaultdict(float)
		amounts[wallet_account] += sign * flt(amount) - flt(fee)
		amounts[bank_account] -= sign * flt(amount)
		amounts[self.get_account("fees_account")] += flt(fee)

		journal_entry = post_wallet_entry(
			company=self.company,
			posting_date=posting_date,
			reference=reference,
			remark=_("Snippe {0} between {1} and the Wallet Account").format(_(transfer_type), bank_account),
			amounts=amounts,
			voucher_type="Contra Entry",
			# The user books it, so their own Journal Entry permissions apply.
			ignore_permissions=False,
		)
		return journal_entry.name

	def validate_bank_account(self, account: str):
		ledger = frappe.get_cached_doc("Account", account)
		if ledger.company != self.company or ledger.is_group or ledger.account_type not in ("Bank", "Cash"):
			frappe.throw(_("{0} must be a Bank or Cash ledger of Company {1}.").format(account, self.company))
		validate_snippe_account_currency(account)

	def get_card_return_urls(self):
		if not self.card_redirect_url:
			frappe.throw(
				_("Set the Card Redirect URL in Snippe Setting {0} before taking card payments.").format(
					self.name
				)
			)

		return {
			"redirect_url": self.card_redirect_url,
			"cancel_url": self.card_cancel_url or self.card_redirect_url,
		}
