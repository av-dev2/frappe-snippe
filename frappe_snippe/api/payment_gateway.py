import math

import frappe
from frappe import _
from frappe.utils import flt

from frappe_snippe.accounting.wallet_entries import add_fee_deduction
from frappe_snippe.api.client import get_snippe_client, get_snippe_setting, snippe_errors_as_messages

GATEWAY_SETTINGS = "Snippe Setting"
CLOSED_REQUEST_STATUSES = ("Paid", "Cancelled")


def get_gateway_name(company: str) -> str:
	return f"Snippe-{company}"


def setup_payment_gateway(setting):
	"""ERPNext Payment Gateway and its account for the company; only where the Payments app is installed."""
	if not setting.enable_collection or "payments" not in frappe.get_installed_apps():
		return

	from payments.utils import create_payment_gateway

	gateway = get_gateway_name(setting.company)
	create_payment_gateway(gateway, settings=GATEWAY_SETTINGS, controller=setting.name)

	# Money paid through the gateway lands in the Snippe balance, so in the Wallet Account.
	gateway_account = frappe.db.get_value("Payment Gateway Account", {"payment_gateway": gateway})
	if gateway_account:
		frappe.db.set_value(
			"Payment Gateway Account", gateway_account, "payment_account", setting.wallet_account
		)
		return

	frappe.get_doc(
		{
			"doctype": "Payment Gateway Account",
			"payment_gateway": gateway,
			"company": setting.company,
			"payment_account": setting.wallet_account,
			"payment_channel": "Email",
			# Payment Requests use the default; another gateway already chosen as default stays so.
			"is_default": not frappe.db.exists(
				"Payment Gateway Account", {"company": setting.company, "is_default": 1}
			),
		}
	).insert(ignore_permissions=True)


def remove_payment_gateway(company: str):
	"""Drop the company's gateway with its Snippe Setting; Payment Requests that used it still block that."""
	gateway = get_gateway_name(company)
	if not frappe.db.exists("Payment Gateway", gateway):
		return

	for gateway_account in frappe.get_all(
		"Payment Gateway Account", {"payment_gateway": gateway}, pluck="name"
	):
		frappe.delete_doc("Payment Gateway Account", gateway_account, ignore_permissions=True)
	frappe.delete_doc("Payment Gateway", gateway, ignore_permissions=True)


def get_checkout_url(payment_request: str) -> str:
	"""Snippe checkout for an unpaid Payment Request; a still open one is reused, so reopening the link is cheap."""
	request = frappe.get_doc("Payment Request", payment_request, for_update=True)
	company = get_open_request_company(request)
	open_checkout = get_open_checkout(request.name)
	if open_checkout:
		return open_checkout

	collection = frappe.get_doc(
		{
			"doctype": "Payment Collection",
			"company": company,
			"customer": request.party,
			"payment_method": "Hosted Session",
			# Snippe charges whole TZS; rounding up keeps the request fully paid.
			"total_amount": math.ceil(flt(request.outstanding_amount)),
			"payment_request": request.name,
		}
	)
	# The payer opening the link is usually a guest.
	collection.flags.ignore_permissions = True
	collection.insert()
	collection.submit()
	# Alerts about the collection go to its owner, who should be the request's creator, not Guest.
	collection.db_set("owner", request.owner, update_modified=False)
	return collection.checkout_url


def get_open_checkout(payment_request: str) -> str | None:
	"""The request's checkout that Snippe still holds open; a closed one gets its Snippe status applied."""
	name = frappe.db.get_value(
		"Payment Collection", {"payment_request": payment_request, "docstatus": 1, "status": "Pending"}
	)
	if not name:
		return None

	from frappe_snippe.api.webhooks import process_inbound_webhook

	collection = frappe.get_doc("Payment Collection", name)
	with get_snippe_client(collection.company) as client, snippe_errors_as_messages():
		status, payload = collection.get_snippe_update(client)
	if status == "Pending":
		return collection.checkout_url

	process_inbound_webhook(collection.name, status, None, payload)
	if frappe.db.get_value("Payment Request", payment_request, "status") in CLOSED_REQUEST_STATUSES:
		refuse_link()
	return None


def get_open_request_company(request) -> str:
	"""The company of an unpaid, inward Snippe Payment Request with collections on; other links are refused."""
	gateway_settings, company = frappe.db.get_value(
		"Payment Gateway", request.payment_gateway, ["gateway_settings", "gateway_controller"]
	) or (None, None)
	if (
		gateway_settings != GATEWAY_SETTINGS
		or company != request.company
		or request.docstatus != 1
		or request.status in CLOSED_REQUEST_STATUSES
		or request.payment_request_type != "Inward"
		or request.party_type != "Customer"
		or not frappe.db.get_value("Snippe Setting", company, "enable_collection")
	):
		refuse_link()
	return company


def refuse_link():
	# One message for every refusal, so a guest cannot learn which requests exist.
	frappe.throw(_("This payment link is not valid."), title=_("Payment Link Not Valid"))


def book_payment_request(collection, paid_amount: float, fee_amount: float) -> bool:
	"""Pay the Payment Request through ERPNext's own Payment Entry, with Snippe's fee deducted, if it still fits."""
	request = frappe.get_doc("Payment Request", collection.payment_request, for_update=True)
	outstanding = flt(request.outstanding_amount)
	# The checkout rounds the request up to whole TZS; anything else is a different payment.
	if request.status in CLOSED_REQUEST_STATUSES or not outstanding <= flt(paid_amount) < outstanding + 1:
		return False

	payment_entry = request.create_payment_entry(submit=False)
	# Cents paid above the request stay unallocated with the customer.
	payment_entry.update({"paid_amount": flt(paid_amount), "reference_no": collection.snippe_reference})
	add_fee_deduction(
		payment_entry, get_snippe_setting(collection.company).get_account("fees_account"), fee_amount
	)
	payment_entry.insert(ignore_permissions=True)
	payment_entry.submit()
	request.make_invoice()
	return True
