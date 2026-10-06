from unittest.mock import MagicMock, patch

import frappe
from frappe.model.document import Document
from frappe.tests.utils import FrappeTestCase
from snippe import Payment

REDIRECT_URL = "https://shop.example/paid"


def make_customer(customer_name="Asha Juma Mrisho"):
	return frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": customer_name,
			"customer_group": "Individual",
			"territory": "All Territories",
		}
	).insert()


def make_contact(customer, first_name, last_name, email):
	return frappe.get_doc(
		{
			"doctype": "Contact",
			"first_name": first_name,
			"last_name": last_name,
			"email_ids": [{"email_id": email, "is_primary": 1}],
			"links": [{"link_doctype": "Customer", "link_name": customer.name}],
		}
	).insert()


def make_address(customer):
	return frappe.get_doc(
		{
			"doctype": "Address",
			"address_title": customer.customer_name,
			"address_type": "Billing",
			"address_line1": "Plot 12, Samora Avenue",
			"address_line2": "Kivukoni",
			"city": "Dar es Salaam",
			"state": "Dar es Salaam",
			"pincode": "11101",
			"country": "Tanzania",
			"links": [{"link_doctype": "Customer", "link_name": customer.name}],
		}
	).insert()


def make_collection(customer, payment_method="Mobile Money"):
	return frappe.get_doc(
		{
			"doctype": "Payment Collection",
			"name": "PAY-COL-TEST",
			"customer": customer.name,
			"payment_method": payment_method,
			"phone_number": "255712345678",
			"total_amount": 1500,
			"idempotency_key": "key-1",
		}
	)


class CustomerTestCase(FrappeTestCase):
	def setUp(self):
		self.customer = make_customer()


class TestCollectionCustomer(CustomerTestCase):
	def test_default_contact_supplies_names_and_email(self):
		make_contact(self.customer, "Neema", "Mushi", "neema@example.com")
		customer = make_collection(self.customer).get_snippe_customer()
		self.assertEqual(
			(customer.firstname, customer.lastname, customer.email, customer.phone),
			("Neema", "Mushi", "neema@example.com", "255712345678"),
		)
		self.assertIsNone(customer.address)

	def test_primary_contact_wins_over_other_contacts(self):
		make_contact(self.customer, "Neema", "Mushi", "neema@example.com")
		primary = make_contact(self.customer, "Baraka", "Kimaro", "baraka@example.com")
		frappe.db.set_value("Customer", self.customer.name, "customer_primary_contact", primary.name)
		customer = make_collection(self.customer).get_snippe_customer()
		self.assertEqual((customer.firstname, customer.email), ("Baraka", "baraka@example.com"))

	def test_customer_name_is_used_without_a_contact(self):
		customer = make_collection(self.customer).get_snippe_customer()
		self.assertEqual(
			(customer.firstname, customer.lastname, customer.email), ("Asha", "Juma Mrisho", None)
		)

	def test_single_word_name_fills_both_names(self):
		single = make_customer("Mwananchi")
		customer = make_collection(single).get_snippe_customer()
		self.assertEqual((customer.firstname, customer.lastname), ("Mwananchi", "Mwananchi"))

	def test_card_customer_includes_address(self):
		make_address(self.customer)
		customer = make_collection(self.customer, "Card").get_snippe_customer()
		self.assertEqual(
			(customer.address, customer.city, customer.state, customer.postcode, customer.country),
			("Plot 12, Samora Avenue, Kivukoni", "Dar es Salaam", "Dar es Salaam", "11101", "TZ"),
		)

	def test_card_customer_without_address_is_rejected(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Add an address"):
			make_collection(self.customer, "Card").get_snippe_customer()


class TestCardReturnUrls(CustomerTestCase):
	def set_urls(self, redirect_url, cancel_url=None):
		settings = frappe.get_single("Snippe Settings")
		settings.update({"card_redirect_url": redirect_url, "card_cancel_url": cancel_url})
		settings.save()
		return settings

	def test_missing_redirect_url_is_rejected(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Card Redirect URL"):
			self.set_urls(None).get_card_return_urls()

	def test_cancel_url_defaults_to_redirect_url(self):
		urls = self.set_urls(REDIRECT_URL).get_card_return_urls()
		self.assertEqual(urls, {"redirect_url": REDIRECT_URL, "cancel_url": REDIRECT_URL})

	@patch.object(Document, "db_set")
	def test_card_payment_sends_urls_and_address(self, db_set):
		make_address(self.customer)
		self.set_urls(REDIRECT_URL, "https://shop.example/cancelled")
		client = MagicMock()
		client.create_card_payment.return_value = Payment.from_dict(
			{"reference": "pi_1", "status": "pending"}
		)
		make_collection(self.customer, "Card").create_payment(client)

		kwargs = client.create_card_payment.call_args.kwargs
		self.assertEqual(kwargs["redirect_url"], REDIRECT_URL)
		self.assertEqual(kwargs["cancel_url"], "https://shop.example/cancelled")
		self.assertEqual((kwargs["customer"].city, kwargs["customer"].country), ("Dar es Salaam", "TZ"))
