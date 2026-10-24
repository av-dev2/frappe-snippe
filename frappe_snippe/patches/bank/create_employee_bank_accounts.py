import frappe

from frappe_snippe.accounting.employee_bank_account import get_bank_by_code, set_default_bank_account
from frappe_snippe.patches.bank import create_snippe_banks
from frappe_snippe.patches.custom_fields import create_custom_fields


def execute():
	"""Give each employee paid by bank a default Bank Account from their Bank Code."""
	# after_migrate makes these, but it runs after patches.
	create_custom_fields.execute()
	create_snippe_banks.execute()

	employees = frappe.get_all(
		"Employee",
		filters={"bank_ac_no": ["is", "set"], "bank_code": ["is", "set"]},
		fields=["name", "employee_name", "bank_ac_no", "bank_code"],
	)
	unknown_codes = []
	for employee in employees:
		bank = get_bank_by_code(employee.bank_code)
		if not bank:
			unknown_codes.append(f"{employee.name}: {employee.bank_code}")
			continue
		set_default_bank_account(employee.name, employee.employee_name, bank, employee.bank_ac_no)

	if unknown_codes:
		frappe.log_error(
			title="frappe_snippe: Employee Bank Code matches no Bank",
			message="\n".join(unknown_codes),
		)
