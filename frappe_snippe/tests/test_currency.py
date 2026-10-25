from unittest.mock import patch

import frappe

from frappe_snippe.api.client import validate_rows_currency, validate_snippe_currency
from frappe_snippe.tests.test_collection_customer import make_customer
from frappe_snippe.tests.test_paid_amount import make_sales_invoice
from frappe_snippe.tests.test_snippe_sdk import make_collection
from frappe_snippe.tests.test_snippe_setting import TZS_COMPANY, TzsCompanyTestCase, make_snippe_setting


def patch_documents(doctype, **values):
	"""Make frappe.get_all return one document of doctype with values; other DocTypes read the database."""
	get_all = frappe.get_all

	def fake_get_all(asked_doctype, *args, **kwargs):
		if asked_doctype == doctype:
			return [frappe._dict(values)]
		return get_all(asked_doctype, *args, **kwargs)

	return patch.object(frappe, "get_all", side_effect=fake_get_all)


class TestSnippeCurrency(TzsCompanyTestCase):
	def setUp(self):
		make_snippe_setting()

	def test_tzs_invoice_can_be_collected(self):
		invoice = make_sales_invoice(make_customer().name, 5000)
		collection = make_collection()
		collection.update({"company": TZS_COMPANY, "references": []})
		collection.append(
			"references",
			{"reference_doctype": "Sales Invoice", "reference_name": invoice.name, "allocated_amount": 5000},
		)
		collection.validate()

	def test_document_in_another_currency_is_rejected(self):
		collection = make_collection()
		collection.company = TZS_COMPANY
		collection.references[0].reference_name = "SINV-USD"
		with (
			patch_documents("Sales Invoice", name="SINV-USD", currency="USD"),
			self.assertRaisesRegex(frappe.ValidationError, "Sales Invoice SINV-USD is in USD"),
		):
			collection.validate()

	def test_document_without_currency_uses_its_company_currency(self):
		self.assertFalse(frappe.get_meta("Journal Entry").has_field("currency"))
		with (
			patch_documents("Journal Entry", name="JV-1", company="Wind Power LLC"),
			self.assertRaisesRegex(frappe.ValidationError, "Journal Entry JV-1 is in USD"),
		):
			validate_snippe_currency("Journal Entry", ["JV-1"])

	def test_rows_are_checked_with_one_query_per_doctype(self):
		rows = [
			frappe._dict(reference_doctype="Sales Invoice", reference_name=name)
			for name in ("SINV-1", "SINV-2", None)
		]
		with patch("frappe_snippe.api.client.validate_snippe_currency") as validate:
			validate_rows_currency(rows)
		validate.assert_called_once_with("Sales Invoice", ["SINV-1", "SINV-2"])

	def test_wallet_account_in_another_currency_is_rejected(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Account Cash - WP is in USD"):
			make_snippe_setting(
				"Wind Power LLC", wallet_account="Cash - WP", fees_account="Administrative Expenses - WP"
			)
