import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class SnippeSettings(Document):
	def validate(self):
		if self.session_expiry_minutes and not 1 <= cint(self.session_expiry_minutes) <= 1440:
			frappe.throw(_("Session Expiry must be between 1 and 1440 minutes."))
		if self.snippe_fees_account:
			root_type, is_group = frappe.db.get_value(
				"Account", self.snippe_fees_account, ["root_type", "is_group"]
			)
			if root_type != "Expense" or is_group:
				frappe.throw(_("Snippe Fees Account must be an Expense account that is not a group."))

	def get_card_return_urls(self):
		if not self.card_redirect_url:
			frappe.throw(_("Set the Card Redirect URL in Snippe Settings before taking card payments."))
		return {
			"redirect_url": self.card_redirect_url,
			"cancel_url": self.card_cancel_url or self.card_redirect_url,
		}
