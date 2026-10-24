import frappe
from erpnext import get_default_cost_center
from frappe import _
from frappe.utils import nowdate

from frappe_snippe.api.client import get_snippe_setting, whole_tzs

# reference doctype -> (payable account field, party type, party field)
PAYABLE_SOURCES = {
	"Purchase Invoice": ("credit_to", "Supplier", "supplier"),
	"Expense Claim": ("payable_account", "Employee", "employee"),
	"Employee Advance": ("advance_account", "Employee", "employee"),
}


def book_completed_payout(disbursement, row):
	"""Book a completed payout and return the row values to save, or None when already booked."""
	if row.accounting_status:
		return None

	fee_lines = get_fee_lines(disbursement, row)
	if row.reference_doctype == "Loan":
		fee_entry = make_journal_entry(disbursement, row, fee_lines)
		return {
			"accounting_status": "Booked",
			"accounting_entry_type": "Loan Disbursement",
			"accounting_entry": make_loan_disbursement_entry(disbursement, row).name,
			"fee_journal_entry": fee_entry and fee_entry.name,
		}

	entry = make_journal_entry(disbursement, row, [get_payable_line(row), *fee_lines])
	if row.reference_doctype == "Salary Slip":
		# Same link HRMS sets for payroll bank entries; it marks the slip as paid.
		frappe.db.set_value("Salary Slip", row.reference_name, "journal_entry", entry.name)

	return {
		"accounting_status": "Booked",
		"accounting_entry_type": "Journal Entry",
		"accounting_entry": entry.name,
	}


def reverse_payout_booking(disbursement, row):
	"""Undo a reversed payout's booking and return the row values to save, or None when not booked."""
	if row.accounting_status != "Booked":
		return None

	if row.accounting_entry_type == "Journal Entry":
		if row.reference_doctype == "Salary Slip":
			frappe.db.set_value("Salary Slip", row.reference_name, "journal_entry", None)

		return {
			"accounting_status": "Reversed",
			**reverse_journal_entry(disbursement, row, "accounting_entry"),
		}

	values = {"accounting_status": "Reversed"}
	loan_disbursement = frappe.get_doc("Loan Disbursement", row.accounting_entry)
	if loan_disbursement.docstatus == 1:
		loan_disbursement.flags.ignore_permissions = True
		loan_disbursement.cancel()
	elif loan_disbursement.docstatus == 0:
		delete_draft(loan_disbursement)
		values["accounting_entry"] = None
	if row.fee_journal_entry:
		values.update(reverse_journal_entry(disbursement, row, "fee_journal_entry"))
	return values


def get_payable_line(row):
	amount = whole_tzs(row.amount)
	if row.reference_doctype == "Salary Slip":
		employee, payroll_entry = frappe.db.get_value(
			"Salary Slip", row.reference_name, ["employee", "payroll_entry"]
		)
		if not payroll_entry:
			frappe.throw(_("Salary Slip {0} has no Payroll Entry to pay against.").format(row.reference_name))
		return {
			"account": frappe.db.get_value("Payroll Entry", payroll_entry, "payroll_payable_account"),
			"party_type": "Employee",
			"party": employee,
			"reference_type": "Payroll Entry",
			"reference_name": payroll_entry,
			"debit_in_account_currency": amount,
		}

	account_field, party_type, party_field = PAYABLE_SOURCES[row.reference_doctype]
	account, party = frappe.db.get_value(
		row.reference_doctype, row.reference_name, [account_field, party_field]
	)
	line = {
		"account": account,
		"party_type": party_type,
		"party": party,
		"reference_type": row.reference_doctype,
		"reference_name": row.reference_name,
		"debit_in_account_currency": amount,
	}
	if row.reference_doctype == "Employee Advance":
		line["is_advance"] = "Yes"
	return line


def get_fee_lines(disbursement, row):
	if not row.fee_amount:
		return []
	return [
		{
			"account": get_snippe_setting(disbursement.company).get_account("fees_account"),
			"cost_center": get_default_cost_center(disbursement.company),
			"debit_in_account_currency": row.fee_amount,
		}
	]


def make_journal_entry(disbursement, row, debit_lines):
	"""Bank Entry that credits the disbursement's paid-from account with the sum of debit_lines."""
	if not debit_lines:
		return None

	today = nowdate()
	journal_entry = frappe.new_doc("Journal Entry")
	journal_entry.update(
		{
			"voucher_type": "Bank Entry",
			"company": disbursement.company,
			"posting_date": today,
			"cheque_no": row.snippe_reference,
			"cheque_date": today,
			"user_remark": get_payout_remark(disbursement, row),
		}
	)
	for line in debit_lines:
		journal_entry.append("accounts", line)
	journal_entry.append(
		"accounts",
		{
			"account": disbursement.paid_from_account,
			"credit_in_account_currency": sum(line["debit_in_account_currency"] for line in debit_lines),
		},
	)
	return save_entry(journal_entry)


def make_loan_disbursement_entry(disbursement, row):
	from lending.loan_management.doctype.loan.loan import make_loan_disbursement

	loan = frappe.db.get_value(
		"Loan", row.reference_name, ["company", "applicant_type", "applicant"], as_dict=True
	)
	entry = make_loan_disbursement(
		row.reference_name,
		loan.company,
		loan.applicant_type,
		loan.applicant,
		pending_amount=whole_tzs(row.amount),
	)
	entry.update(
		{
			"disbursement_account": disbursement.paid_from_account,
			"reference_number": row.snippe_reference,
			"reference_date": nowdate(),
		}
	)
	return save_entry(entry)


def reverse_journal_entry(disbursement, row, link_field):
	"""Reverse the Journal Entry in row.<link_field>; a draft never posted, so it is deleted."""
	from erpnext.accounts.doctype.journal_entry.journal_entry import make_reverse_journal_entry

	journal_entry = frappe.get_doc("Journal Entry", row.get(link_field))
	if journal_entry.docstatus == 0:
		delete_draft(journal_entry)
		return {link_field: None}

	today = nowdate()
	reversal = make_reverse_journal_entry(journal_entry.name)
	reversal.update(
		{
			"posting_date": today,
			"cheque_no": row.snippe_reference,
			"cheque_date": today,
			"user_remark": _("Reversal of {0}").format(get_payout_remark(disbursement, row)),
		}
	)
	return {"reversal_journal_entry": save_entry(reversal).name}


def get_payout_remark(disbursement, row):
	return _("Snippe payout {0} for {1} row {2}").format(row.snippe_reference, disbursement.name, row.idx)


def delete_draft(entry):
	# force skips the link check; the caller clears the row's link to this draft.
	frappe.delete_doc(entry.doctype, entry.name, ignore_permissions=True, force=True)


def save_entry(entry):
	entry.insert(ignore_permissions=True)
	if get_snippe_setting(entry.company).auto_submit_accounting_entries:
		entry.submit()
	return entry
