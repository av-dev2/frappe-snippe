import frappe
from frappe import _
from frappe.model.document import Document


class SnippeSettings(Document):
	def get_card_return_urls(self):
		if not self.card_redirect_url:
			frappe.throw(_("Set the Card Redirect URL in Snippe Settings before taking card payments."))
		return {
			"redirect_url": self.card_redirect_url,
			"cancel_url": self.card_cancel_url or self.card_redirect_url,
		}
