import dataclasses

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_url
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
			return client.create_card_payment(redirect_url=get_url(self.get_url()), **payment_kwargs)
		return client.create_qr_payment(**payment_kwargs)

	def get_snippe_customer(self):
		firstname, _separator, lastname = (self.customer_name or self.customer).strip().partition(" ")
		return Customer(firstname=firstname, lastname=lastname.strip() or firstname, phone=self.phone_number)
