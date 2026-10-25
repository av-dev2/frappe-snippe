import dataclasses

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from frappe_snippe.api.client import (
	get_snippe_client,
	snippe_errors_as_messages,
	validate_snippe_account_currency,
)


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

	@frappe.whitelist()
	def get_balance(self):
		"""Live Snippe balance for the form; it is never stored."""
		with get_snippe_client(self.company) as client, snippe_errors_as_messages():
			return dataclasses.asdict(client.get_balance())

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
