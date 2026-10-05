import frappe
from snippe import Snippe


def get_snippe_client():
	settings = frappe.get_single("Snippe Settings")
	if not settings.enabled:
		frappe.throw(frappe._("Snippe integration is not enabled."))

	if not settings.api_key:
		frappe.throw(frappe._("Snippe API Key is missing in Settings."))

	api_url = settings.api_url or ""
	return Snippe(api_key=settings.get_password("api_key"), base_url=api_url)
