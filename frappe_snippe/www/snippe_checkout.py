import frappe
from frappe.utils.verified_command import verify_request

from frappe_snippe.api.payment_gateway import get_checkout_url, refuse_link

no_cache = 1


def get_context(context):
	"""Send the payer of a Payment Request's link on to Snippe's checkout."""
	# Payment Request names are sequential, so only a link signed by this site is served.
	payment_request = frappe.form_dict.get("payment_request")
	if (
		not verify_request()
		or not isinstance(payment_request, str)
		or not frappe.db.exists("Payment Request", payment_request)
	):
		refuse_link()

	frappe.local.flags.redirect_location = get_checkout_url(payment_request)
	# A GET request is rolled back by default; keep the Payment Collection that holds the open checkout.
	frappe.local.flags.commit = True
	raise frappe.Redirect
