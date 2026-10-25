import frappe
from erpnext import get_default_cost_center
from frappe.utils import flt


def post_wallet_entry(
	*,
	company: str,
	posting_date: str,
	reference: str,
	remark: str,
	amounts: dict,
	voucher_type: str = "Bank Entry",
	ignore_permissions: bool = True,
):
	"""Submit a Journal Entry from {account: debit (+) or credit (-)}; None when nothing is left.

	Amounts on the same account cancel out, so callers can add up movements without merging them.
	"""
	amounts = {account: amount for account, amount in amounts.items() if amount}
	if not amounts:
		return None

	journal_entry = frappe.new_doc("Journal Entry")
	journal_entry.update(
		{
			"voucher_type": voucher_type,
			"company": company,
			"posting_date": posting_date,
			"cheque_no": reference,
			"cheque_date": posting_date,
			"user_remark": remark,
		}
	)
	cost_center = get_default_cost_center(company)
	for account, amount in amounts.items():
		journal_entry.append(
			"accounts",
			{
				"account": account,
				"cost_center": cost_center,
				"debit_in_account_currency": max(amount, 0),
				"credit_in_account_currency": max(-amount, 0),
			},
		)
	journal_entry.flags.ignore_permissions = ignore_permissions
	journal_entry.insert()
	journal_entry.submit()
	return journal_entry


def add_fee_deduction(payment_entry, fees_account: str, fee_amount: float):
	"""Snippe keeps its fee, so the Wallet Account receives the net amount and the fee is a deduction."""
	net_amount = flt(payment_entry.paid_amount) - flt(fee_amount)
	payment_entry.update({"paid_amount": net_amount, "received_amount": net_amount})
	if fee_amount:
		payment_entry.append(
			"deductions",
			{
				"account": fees_account,
				"cost_center": get_default_cost_center(payment_entry.company),
				"amount": fee_amount,
			},
		)
