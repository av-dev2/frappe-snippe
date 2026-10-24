import frappe
from frappe import _


def is_paid_by_bank(employee) -> bool:
	return employee.salary_mode == "Bank" and bool(employee.bank_ac_no)


def get_bank_by_code(bank_code: str) -> str | None:
	return frappe.db.get_value("Bank", {"bank_code": bank_code})


def validate_bank_code(employee, method=None):
	"""An employee paid by bank needs a Bank Code that names a Bank."""
	if not is_paid_by_bank(employee):
		return

	if not employee.bank_code:
		frappe.throw(
			_("Set the Bank Code for Bank A/C No. {0}.").format(employee.bank_ac_no),
			title=_("Missing Bank Code"),
		)
	if not get_bank_by_code(employee.bank_code):
		frappe.throw(
			_("No Bank has the Bank Code {0}. Set it on the Bank first.").format(employee.bank_code),
			title=_("Unknown Bank Code"),
		)


def sync_bank_account(employee, method=None):
	if is_paid_by_bank(employee):
		set_default_bank_account(
			employee.name,
			employee.employee_name,
			get_bank_by_code(employee.bank_code),
			employee.bank_ac_no,
		)


def set_default_bank_account(employee: str, employee_name: str, bank: str, bank_account_no: str):
	"""Make this the employee's default Bank Account, so disbursements pay into it."""
	filters = {
		"party_type": "Employee",
		"party": employee,
		"bank": bank,
		"bank_account_no": bank_account_no,
	}
	name = frappe.db.get_value("Bank Account", filters)
	if not name:
		frappe.get_doc(
			{
				"doctype": "Bank Account",
				"account_name": f"{employee_name} - {bank_account_no}",
				"is_default": 1,
				**filters,
			}
		).insert(ignore_permissions=True)
		return

	account = frappe.get_doc("Bank Account", name)
	if account.is_default and not account.disabled:
		return
	account.is_default = 1
	account.disabled = 0
	account.save(ignore_permissions=True)
