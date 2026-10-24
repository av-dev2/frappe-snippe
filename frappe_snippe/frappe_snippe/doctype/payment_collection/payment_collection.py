import dataclasses

import frappe
from frappe import _
from frappe.contacts.doctype.address.address import get_default_address
from frappe.contacts.doctype.contact.contact import get_default_contact
from frappe.model.document import Document
from frappe.utils import cint, flt
from snippe import Customer

from frappe_snippe.api.client import (
	PAYMENT_STATUS,
	SESSION_STATUS,
	SNIPPE_CURRENCY,
	get_snippe_client,
	get_snippe_setting,
	is_valid_idempotency_key,
	new_idempotency_key,
	normalize_phone,
	snippe_errors_as_messages,
	to_snippe_amount,
	validate_rows_currency,
	whole_tzs,
)
from frappe_snippe.api.webhooks import get_webhook_url


class PaymentCollection(Document):
	def before_insert(self):
		# calculate total amount
		self.total_amount = sum(row.allocated_amount for row in self.references)

	def validate(self):
		if self.company:
			get_snippe_setting(self.company).validate_collection_enabled()
		if not is_valid_idempotency_key(self.idempotency_key):
			self.idempotency_key = new_idempotency_key()
		if self.phone_number:
			self.phone_number = normalize_phone(self.phone_number)
		if self.allow_custom_amount and self.payment_method != "Hosted Session":
			frappe.throw(_("Allow Custom Amount works only with the Hosted Session payment method."))

		validate_rows_currency(self.references)

	def on_submit(self):
		self.process_collection()

	def on_cancel(self):
		if self.payment_method == "Hosted Session" and self.status == "Pending" and self.snippe_reference:
			with get_snippe_client(self.company) as client, snippe_errors_as_messages():
				client.cancel_session(self.snippe_reference)
			self.db_set("status", "Voided")

	def process_collection(self):
		is_session = self.payment_method == "Hosted Session"
		if not is_session and not self.phone_number:
			frappe.throw(_("Phone Number is required for {0}.").format(_(self.payment_method)))

		with get_snippe_client(self.company) as client, snippe_errors_as_messages():
			values = self.create_session(client) if is_session else self.create_payment(client)
		self.db_set(values)

		for row in self.references:
			row.db_set("status", "Pending")

	def create_session(self, client):
		"""Open a Snippe hosted checkout for the collection total."""
		setting = get_snippe_setting(self.company)
		session = client.create_session(
			to_snippe_amount(self.total_amount),
			description=_("Payment for {0}").format(self.name),
			customer=self.get_session_customer(),
			line_items=self.get_session_line_items(),
			display={"show_line_items": True},
			redirect_url=setting.session_redirect_url or None,
			webhook_url=get_webhook_url(),
			metadata={"erpnext_doc": self.name, "company": self.company},
			expires_in=cint(setting.session_expiry_minutes) * 60 or None,
			idempotency_key=self.idempotency_key,
			**self.get_custom_amount_limits(),
		)
		return {
			"raw_response": frappe.as_json(session),
			"snippe_reference": session["reference"],
			"status": SESSION_STATUS.get(session["status"], "Pending"),
			"checkout_url": session["checkout_url"],
			"payment_link_url": session.get("payment_link_url") or session["checkout_url"],
		}

	def get_snippe_update(self, client) -> tuple[str, dict]:
		"""Snippe's current status and a webhook-shaped payload, for a collection whose webhook never came."""
		if self.payment_method != "Hosted Session":
			payment = client.get_payment_data(self.snippe_reference)
			return PAYMENT_STATUS.get(payment["status"], "Pending"), payment

		session = client.get_session(self.snippe_reference)
		# A custom-amount session reports the suggested amount, not what the customer paid.
		amount = (
			None
			if self.allow_custom_amount
			else {"value": session["amount"], "currency": session["currency"]}
		)
		return SESSION_STATUS.get(session["status"], "Pending"), {"amount": amount}

	def get_custom_amount_limits(self):
		if not self.allow_custom_amount:
			return {}
		return {
			"allow_custom_amount": True,
			"min_amount": whole_tzs(self.min_amount) or None,
			"max_amount": whole_tzs(self.max_amount) or None,
		}

	def get_reference_allocations(self, paid_amount):
		"""Split a received amount across references in row order; any excess stays unallocated."""
		precision = self.precision("total_amount")
		allocations = []
		remaining = flt(paid_amount, precision)
		for row in self.references:
			amount = min(flt(row.allocated_amount, precision), remaining)
			if amount <= 0:
				break
			allocations.append((row, amount))
			remaining = flt(remaining - amount, precision)
		return allocations

	def get_session_customer(self):
		customer = {
			"name": self.customer_name or self.customer,
			"phone": self.phone_number,
			"email": self.get_snippe_customer().email,
		}
		return {key: value for key, value in customer.items() if value}

	def get_session_line_items(self):
		return [
			{
				"id": row.reference_name,
				"name": f"{_(row.reference_doctype)} {row.reference_name}",
				"quantity": 1,
				"unit_price": whole_tzs(row.allocated_amount),
			}
			for row in self.references
		]

	def create_payment(self, client):
		payment_kwargs = {
			"amount": to_snippe_amount(self.total_amount),
			"currency": SNIPPE_CURRENCY,
			"phone_number": self.phone_number,
			"customer": self.get_snippe_customer(),
			"metadata": {"erpnext_doc": self.name, "company": self.company},
			"idempotency_key": self.idempotency_key,
			"webhook_url": get_webhook_url(),
		}
		if self.payment_method == "Mobile Money":
			payment = client.create_mobile_payment(**payment_kwargs)
		elif self.payment_method == "Card":
			card_return_urls = get_snippe_setting(self.company).get_card_return_urls()
			payment = client.create_card_payment(**card_return_urls, **payment_kwargs)
		else:
			payment = client.create_qr_payment(**payment_kwargs)

		return {
			"raw_response": frappe.as_json(dataclasses.asdict(payment)),
			"snippe_reference": payment.reference,
			"status": PAYMENT_STATUS.get(payment.status, "Pending"),
			"payment_url": payment.payment_url,
			"payment_qr_code": payment.payment_qr_code or payment.qr_code,
		}

	def get_snippe_customer(self):
		"""Build the Snippe Customer from the customer's primary or default Contact."""
		customer = frappe.db.get_value(
			"Customer",
			self.customer,
			["customer_name", "customer_primary_contact", "customer_primary_address", "email_id"],
			as_dict=True,
		)
		contact_name = customer.customer_primary_contact or get_default_contact("Customer", self.customer)
		contact = frappe._dict()
		if contact_name:
			contact = frappe.db.get_value(
				"Contact", contact_name, ["first_name", "last_name", "email_id"], as_dict=True
			)

		firstname, lastname = contact.first_name, contact.last_name
		if not firstname:
			firstname, _separator, lastname = customer.customer_name.strip().partition(" ")
		address = (
			self.get_snippe_address(customer.customer_primary_address)
			if self.payment_method == "Card"
			else {}
		)
		return Customer(
			firstname=firstname,
			lastname=(lastname or "").strip() or firstname,
			email=contact.email_id or customer.email_id or None,
			phone=self.phone_number,
			**address,
		)

	def get_snippe_address(self, address_name):
		address_name = address_name or get_default_address("Customer", self.customer)
		if not address_name:
			frappe.throw(
				_("Add an address to Customer {0} before taking card payments.").format(self.customer)
			)

		address = frappe.db.get_value(
			"Address",
			address_name,
			["address_line1", "address_line2", "city", "state", "pincode", "country"],
			as_dict=True,
		)
		country_code = (
			frappe.get_cached_value("Country", address.country, "code") if address.country else None
		)
		return {
			"address": ", ".join(filter(None, (address.address_line1, address.address_line2))),
			"city": address.city,
			"state": address.state,
			"postcode": address.pincode,
			"country": country_code.upper() if country_code else None,
		}
