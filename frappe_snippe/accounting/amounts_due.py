import frappe
from frappe.utils import flt

# reference doctype -> fields that tell how much the ledger still shows as owed
LEDGER_DUE_FIELDS = {
	"Purchase Invoice": ["outstanding_amount"],
	"Expense Claim": ["grand_total", "total_amount_reimbursed"],
	"Employee Advance": ["advance_amount", "paid_amount"],
	"Salary Slip": ["rounded_total", "net_pay", "journal_entry"],
	"Loan": ["loan_amount", "disbursed_amount"],
}


def get_amounts_left_to_pay(reference_doctype, reference_names, exclude_disbursement):
	"""Return {name: amount} still payable, net of Snippe payouts the ledger does not show yet."""
	left_to_pay = {
		values.name: get_ledger_amount_due(reference_doctype, values)
		for values in frappe.get_all(
			reference_doctype,
			filters={"name": ("in", reference_names)},
			fields=["name", *LEDGER_DUE_FIELDS[reference_doctype]],
		)
	}
	for row in get_unposted_payouts(reference_doctype, reference_names, exclude_disbursement):
		left_to_pay[row.reference_name] = left_to_pay.get(row.reference_name, 0) - flt(row.amount)
	return left_to_pay


def get_ledger_amount_due(reference_doctype, values):
	if reference_doctype == "Purchase Invoice":
		return flt(values.outstanding_amount)
	if reference_doctype == "Expense Claim":
		return flt(values.grand_total) - flt(values.total_amount_reimbursed)
	if reference_doctype == "Employee Advance":
		return flt(values.advance_amount) - flt(values.paid_amount)
	if reference_doctype == "Salary Slip":
		return 0 if values.journal_entry else flt(values.rounded_total or values.net_pay)
	return flt(values.loan_amount) - flt(values.disbursed_amount)


def get_unposted_payouts(reference_doctype, reference_names, exclude_disbursement):
	"""Active payout rows in other submitted disbursements whose entry is not in the ledger yet."""
	rows = frappe.get_all(
		"Payment Disbursement Item",
		filters={
			"docstatus": 1,
			"parent": ("!=", exclude_disbursement),
			"reference_doctype": reference_doctype,
			"reference_name": ("in", reference_names),
			"status": ("not in", ("Failed", "Reversed")),
		},
		fields=["reference_name", "amount", "accounting_status", "accounting_entry_type", "accounting_entry"],
	)
	posted = get_submitted_entries(rows)
	# A booked Salary Slip already carries its Journal Entry, which zeroes its ledger amount.
	return [
		row
		for row in rows
		if not (
			row.accounting_status == "Booked"
			and (
				reference_doctype == "Salary Slip"
				or (row.accounting_entry_type, row.accounting_entry) in posted
			)
		)
	]


def get_submitted_entries(rows):
	names_by_type = {}
	for row in rows:
		if row.accounting_status == "Booked":
			names_by_type.setdefault(row.accounting_entry_type, []).append(row.accounting_entry)
	return {
		(entry_type, name)
		for entry_type, names in names_by_type.items()
		for name in frappe.get_all(entry_type, filters={"name": ("in", names), "docstatus": 1}, pluck="name")
	}
