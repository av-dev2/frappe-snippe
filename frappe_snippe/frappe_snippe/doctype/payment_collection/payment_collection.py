import dataclasses

import frappe
from frappe import _
from frappe.contacts.doctype.address.address import get_default_address
from frappe.contacts.doctype.contact.contact import get_default_contact
from frappe.model.document import Document
from snippe import AuthenticationError, ConflictError, Customer, RateLimitError, ValidationError

from frappe_snippe.api.client import (
	PAYMENT_STATUS,
	get_snippe_client,
	is_valid_idempotency_key,
	new_idempotency_key,
	normalize_phone,
	to_snippe_amount,
)
from frappe_snippe.api.webhooks import get_webhook_url


class PaymentCollection(Document):
	def before_insert(self):
		# calculate total amount
		self.total_amount = sum(row.allocated_amount for row in self.references)

	def validate(self):
		if not is_valid_idempotency_key(self.idempotency_key):
			self.idempotency_key = new_idempotency_key()
		if self.phone_number:
			self.phone_number = normalize_phone(self.phone_number)

	def on_submit(self):
		self.process_collection()

	def process_collection(self):
		if self.payment_method == "Hosted Session":
			frappe.throw(_("Hosted Session is not supported by the Snippe Python SDK."))
		if not self.phone_number:
			frappe.throw(_("Phone Number is required for {0}.").format(_(self.payment_method)))

		try:
			response = self.create_payment(get_snippe_client())
		except (ValidationError, ConflictError, RateLimitError, AuthenticationError) as error:
			frappe.throw(
				_("Snippe rejected the payment: {0}").format(error.message),
				title=_("Snippe Payment Rejected"),
			)

		self.db_set(
			{
				"raw_response": frappe.as_json(dataclasses.asdict(response)),
				"snippe_reference": response.reference,
				"status": PAYMENT_STATUS.get(response.status, "Pending"),
				"payment_url": response.payment_url,
				"payment_qr_code": response.payment_qr_code or response.qr_code,
			}
		)

		for row in self.references:
			row.db_set("status", "Pending")

	def create_payment(self, client):
		payment_kwargs = {
			"amount": to_snippe_amount(self.total_amount),
			"currency": "TZS",
			"phone_number": self.phone_number,
			"customer": self.get_snippe_customer(),
			"metadata": {"erpnext_doc": self.name, "company": self.company},
			"idempotency_key": self.idempotency_key,
			"webhook_url": get_webhook_url(),
		}
		if self.payment_method == "Mobile Money":
			return client.create_mobile_payment(**payment_kwargs)
		if self.payment_method == "Card":
			card_return_urls = frappe.get_cached_doc("Snippe Settings").get_card_return_urls()
			return client.create_card_payment(**card_return_urls, **payment_kwargs)
		return client.create_qr_payment(**payment_kwargs)

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
