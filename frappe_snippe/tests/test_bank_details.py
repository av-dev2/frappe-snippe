import typing
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from snippe.types import BankCode

from frappe_snippe.patches.bank import create_snippe_banks
from frappe_snippe.tests.test_employee_bank_account import make_employee

SDK_BANK_CODES = list(typing.get_args(BankCode))


def make_bank_row(**values):
	return frappe.get_doc(
		{
			"doctype": "Payment Disbursement Item",
			"idx": 1,
			"reference_doctype": "Purchase Invoice",
			"reference_name": "ACC-PINV-TEST",
			"recipient_name": "Bank Row Supplier",
			"channel": "Bank Transfer",
			**values,
		}
	)


class TestSnippeBanks(FrappeTestCase):
	def setUp(self):
		frappe.db.delete("Bank Account")
		frappe.db.delete("Bank")

	def bank_codes(self):
		return frappe.get_all("Bank", pluck="bank_code", order_by="bank_code")

	def test_seed_creates_each_bank_once(self):
		create_snippe_banks.execute()
		create_snippe_banks.execute()

		self.assertEqual(self.bank_codes(), SDK_BANK_CODES)
		self.assertEqual(
			frappe.db.get_value("Bank", "CRDB BANK PLC", ["bank_code", "swift_number"]),
			("CRDB", "CORUTZTZ"),
		)

	def test_seed_skips_a_bank_created_under_another_name(self):
		frappe.get_doc(
			doctype="Bank",
			bank_name="Exim Bank (T) Limited",
			swift_number="EXTNTZTZ",
			bank_code="EXIM",
		).insert()
		frappe.get_doc(doctype="Bank", bank_name="crdb bank plc", bank_code="NMB").insert()

		create_snippe_banks.execute()

		self.assertEqual(self.bank_codes().count("EXIM"), 1)
		self.assertFalse(frappe.db.exists("Bank", {"bank_code": "CRDB"}))

	def test_bank_needs_a_bank_code(self):
		with self.assertRaises(frappe.MandatoryError):
			frappe.get_doc(doctype="Bank", bank_name="Codeless Bank").insert()


class TestPartyBankDetails(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.db.delete("Bank Account")
		frappe.db.delete("Bank")
		create_snippe_banks.execute()
		cls.supplier = frappe.get_doc(
			doctype="Supplier", supplier_name="Bank Row Supplier", supplier_type="Company"
		).insert()
		cls.employee = make_employee("Bank Row Employee")

	def setUp(self):
		frappe.db.delete("Bank Account")

	def make_bank_account(self, **values):
		return frappe.get_doc(
			{
				"doctype": "Bank Account",
				"account_name": "Bank Row Supplier",
				"bank": "CRDB BANK PLC",
				"bank_account_no": "0150123456700",
				"party_type": "Supplier",
				"party": self.supplier.name,
				"is_default": 1,
				**values,
			}
		).insert()

	def fill(self, row, party):
		"""Fill the row as if its reference document names this party."""
		get_value = frappe.db.get_value

		def get_reference_party(doctype, *args, **kwargs):
			if doctype == row.reference_doctype:
				return party
			return get_value(doctype, *args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=get_reference_party):
			row.set_party_bank_details()
		return row

	def test_fills_bank_code_and_account_from_supplier(self):
		self.make_bank_account()
		row = self.fill(make_bank_row(), self.supplier.name)
		self.assertEqual(
			(row.bank, row.bank_code, row.bank_account), ("CRDB BANK PLC", "CRDB", "0150123456700")
		)

	def test_keeps_bank_details_the_user_entered(self):
		self.make_bank_account()
		row = self.fill(make_bank_row(bank_code="NMB", bank_account="2041000111"), self.supplier.name)
		self.assertEqual((row.bank, row.bank_code, row.bank_account), (None, "NMB", "2041000111"))

	def test_supplier_without_bank_account_leaves_row_blank(self):
		row = self.fill(make_bank_row(), self.supplier.name)
		self.assertEqual((row.bank, row.bank_code, row.bank_account), (None, None, None))

	def test_company_bank_account_on_supplier_is_not_used(self):
		# Supplier.default_bank_account is the company's own account, not the payee's.
		company_account = self.make_bank_account(party_type=None, party=None, is_default=0)
		self.supplier.db_set("default_bank_account", company_account.name)
		self.assertIsNone(self.fill(make_bank_row(), self.supplier.name).bank_account)

	def test_fills_employee_documents_from_the_employee(self):
		self.make_bank_account(
			bank="NATIONAL MICROFINANCE BANK LIMITED",
			party_type="Employee",
			party=self.employee.name,
			bank_account_no="2041000111",
		)
		for reference_doctype in ("Salary Slip", "Expense Claim", "Employee Advance"):
			with self.subTest(reference_doctype=reference_doctype):
				row = self.fill(make_bank_row(reference_doctype=reference_doctype), self.employee.name)
				self.assertEqual((row.bank_code, row.bank_account), ("NMB", "2041000111"))

	def test_fills_loan_from_the_applicant(self):
		self.make_bank_account(party_type="Employee", party=self.employee.name)
		row = self.fill(make_bank_row(reference_doctype="Loan"), ("Employee", self.employee.name))
		self.assertEqual((row.bank_code, row.bank_account), ("CRDB", "0150123456700"))


class TestBankDetailsValidation(FrappeTestCase):
	def test_bank_transfer_needs_bank_code_and_account(self):
		for values in ({"bank_account": "2041000111"}, {"bank_code": "NMB"}):
			with self.subTest(values=values), self.assertRaises(frappe.ValidationError):
				make_bank_row(**values).validate_bank_details()

	def test_bank_code_must_be_a_snippe_code(self):
		with self.assertRaisesRegex(frappe.ValidationError, "does not support"):
			make_bank_row(bank_code="NOTABANK", bank_account="2041000111").validate_bank_details()

	def test_complete_bank_details_pass(self):
		make_bank_row(bank_code="NMB", bank_account="2041000111").validate_bank_details()

	def test_disbursement_rejects_bank_row_without_details(self):
		disbursement = frappe.get_doc(
			{
				"doctype": "Payment Disbursement",
				"items": [{"reference_doctype": "Expense Claim", "channel": "Bank Transfer", "amount": 5000}],
			}
		)
		with self.assertRaisesRegex(frappe.ValidationError, "Bank Code and Bank Account"):
			disbursement.validate()
