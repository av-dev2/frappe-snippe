import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_snippe.accounting.employee_bank_account import validate_bank_code
from frappe_snippe.patches.bank import create_employee_bank_accounts, create_snippe_banks

UNKNOWN_CODE_LOG = "frappe_snippe: Employee Bank Code matches no Bank"


def make_employee(first_name, **values):
	return frappe.get_doc(
		{
			"doctype": "Employee",
			"first_name": first_name,
			"gender": "Female",
			"date_of_birth": "1990-05-08",
			"date_of_joining": "2020-01-01",
			"company": frappe.get_all("Company", pluck="name", limit=1)[0],
			**values,
		}
	).insert()


def bank_employee(**values):
	return {"salary_mode": "Bank", "bank_ac_no": "0150123456700", "bank_code": "CRDB", **values}


class TestEmployeeBankAccount(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.db.delete("Bank Account")
		frappe.db.delete("Bank")
		create_snippe_banks.execute()

	def setUp(self):
		frappe.db.delete("Bank Account")

	def bank_accounts(self, employee):
		return frappe.get_all(
			"Bank Account",
			filters={"party_type": "Employee", "party": employee.name},
			fields=["bank", "bank_account_no", "is_default"],
			order_by="creation",
		)

	def test_bank_salary_needs_a_bank_code(self):
		employee = frappe._dict(bank_employee(bank_code=None))
		with self.assertRaisesRegex(frappe.ValidationError, "Set the Bank Code"):
			validate_bank_code(employee)

	def test_unknown_bank_code_is_rejected(self):
		with self.assertRaisesRegex(frappe.ValidationError, "No Bank has the Bank Code NOTABANK"):
			make_employee("Unknown Code", **bank_employee(bank_code="NOTABANK"))

	def test_cash_salary_needs_no_bank_code(self):
		employee = make_employee("Cash Paid", salary_mode="Cash", bank_ac_no="0150123456700")
		self.assertEqual(self.bank_accounts(employee), [])

	def test_save_creates_one_default_bank_account(self):
		employee = make_employee("Bank Paid", **bank_employee())
		employee.save()

		self.assertEqual(
			self.bank_accounts(employee),
			[{"bank": "CRDB BANK PLC", "bank_account_no": "0150123456700", "is_default": 1}],
		)

	def test_latest_account_becomes_the_default(self):
		employee = make_employee("Bank Changed", **bank_employee())
		employee.update({"bank_ac_no": "2041000111", "bank_code": "NMB"})
		employee.save()
		self.assertEqual([a.is_default for a in self.bank_accounts(employee)], [0, 1])

		employee.update(bank_employee())
		employee.save()
		self.assertEqual([a.is_default for a in self.bank_accounts(employee)], [1, 0])

	def test_patch_creates_accounts_and_logs_unknown_codes(self):
		known = make_employee("Patch Known")
		known.db_set(bank_employee())
		unknown = make_employee("Patch Unknown")
		unknown.db_set(bank_employee(bank_code="NOTABANK"))
		frappe.db.delete("Error Log", {"method": UNKNOWN_CODE_LOG})

		create_employee_bank_accounts.execute()

		self.assertEqual(len(self.bank_accounts(known)), 1)
		self.assertEqual(self.bank_accounts(unknown), [])
		self.assertIn(
			f"{unknown.name}: NOTABANK",
			frappe.db.get_value("Error Log", {"method": UNKNOWN_CODE_LOG}, "error"),
		)
