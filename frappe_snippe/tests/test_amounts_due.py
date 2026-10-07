from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_snippe.accounting import amounts_due, disbursement_entries
from frappe_snippe.accounting.amounts_due import get_amounts_left_to_pay, get_ledger_amount_due
from frappe_snippe.frappe_snippe.doctype.payment_disbursement import payment_disbursement


def payout_row(reference_name, amount, accounting_status=None, entry=None, entry_type="Journal Entry"):
	return frappe._dict(
		reference_name=reference_name,
		amount=amount,
		accounting_status=accounting_status,
		accounting_entry_type=entry_type if entry else None,
		accounting_entry=entry,
	)


class TestLedgerAmountDue(FrappeTestCase):
	def test_amount_due_per_document_type(self):
		cases = [
			("Purchase Invoice", {"outstanding_amount": 30000}, 30000),
			("Expense Claim", {"grand_total": 50000, "total_amount_reimbursed": 20000}, 30000),
			("Employee Advance", {"advance_amount": 80000, "paid_amount": 80000}, 0),
			("Salary Slip", {"rounded_total": 450000, "net_pay": 449999.6}, 450000),
			("Salary Slip", {"rounded_total": 0, "net_pay": 449999.6}, 449999.6),
			("Salary Slip", {"rounded_total": 450000, "journal_entry": "ACC-JV-0001"}, 0),
			("Loan", {"loan_amount": 1000000, "disbursed_amount": 400000}, 600000),
		]
		for doctype, values, expected in cases:
			self.assertEqual(get_ledger_amount_due(doctype, frappe._dict(values)), expected, doctype)


class TestAmountsLeftToPay(FrappeTestCase):
	def left_to_pay(self, doctype, ledger_rows, payout_rows, submitted=()):
		def get_all(target, filters=None, fields=None, pluck=None):
			if target == doctype:
				return ledger_rows
			if target == "Payment Disbursement Item":
				return payout_rows
			return [name for name in filters["name"][1] if name in submitted]

		with patch.object(amounts_due.frappe, "get_all", side_effect=get_all):
			return get_amounts_left_to_pay(doctype, ["PINV-1"], "PAY-DIS-NEW")

	def test_unposted_payouts_in_other_disbursements_reduce_what_is_left(self):
		ledger = [frappe._dict(name="PINV-1", outstanding_amount=100000)]
		payouts = [
			payout_row("PINV-1", 30000),  # sent, not booked yet
			payout_row("PINV-1", 20000, "Booked", "ACC-JV-DRAFT"),  # booked as a draft entry
			payout_row("PINV-1", 40000, "Booked", "ACC-JV-POSTED"),  # already in outstanding_amount
		]
		left = self.left_to_pay("Purchase Invoice", ledger, payouts, submitted={"ACC-JV-POSTED"})
		self.assertEqual(left, {"PINV-1": 50000})

	def test_booked_salary_payouts_are_already_counted_by_the_slip(self):
		ledger = [frappe._dict(name="PINV-1", rounded_total=0, net_pay=0, journal_entry="ACC-JV-DRAFT")]
		payouts = [payout_row("PINV-1", 450000, "Booked", "ACC-JV-DRAFT")]
		self.assertEqual(self.left_to_pay("Salary Slip", ledger, payouts), {"PINV-1": 0})

	def test_payout_query_skips_failed_reversed_and_this_disbursement(self):
		with patch.object(amounts_due.frappe, "get_all", return_value=[]) as get_all:
			get_amounts_left_to_pay("Purchase Invoice", ["PINV-1"], "PAY-DIS-NEW")
		filters = get_all.call_args_list[1].kwargs["filters"]
		self.assertEqual(filters["status"], ("not in", ("Failed", "Reversed")))
		self.assertEqual((filters["docstatus"], filters["parent"]), (1, ("!=", "PAY-DIS-NEW")))


class TestDisbursementAmountsDue(FrappeTestCase):
	def make_disbursement(self, *rows):
		return frappe.get_doc(
			{
				"doctype": "Payment Disbursement",
				"name": "PAY-DIS-NEW",
				"company": "Wind Power LLC",
				"items": [
					{"reference_doctype": "Purchase Invoice", "reference_name": name, "amount": amount}
					for name, amount in rows
				],
			}
		)

	def validate(self, disbursement, left_to_pay):
		with patch.object(
			payment_disbursement, "get_amounts_left_to_pay", return_value=left_to_pay
		) as get_left:
			disbursement.validate_amounts_due()
		return get_left

	def test_paying_exactly_what_is_left_is_allowed(self):
		get_left = self.validate(self.make_disbursement(("PINV-1", 50000)), {"PINV-1": 50000})
		get_left.assert_called_once_with("Purchase Invoice", ["PINV-1"], "PAY-DIS-NEW")

	def test_paying_more_than_is_left_is_blocked(self):
		with self.assertRaisesRegex(frappe.ValidationError, "PINV-1 has .* left to pay"):
			self.validate(self.make_disbursement(("PINV-1", 50000.01)), {"PINV-1": 50000})

	def test_rows_for_the_same_document_are_added_up(self):
		disbursement = self.make_disbursement(("PINV-1", 30000), ("PINV-1", 30000))
		with self.assertRaisesRegex(frappe.ValidationError, "PINV-1"):
			self.validate(disbursement, {"PINV-1": 50000})

	def test_fully_paid_document_is_blocked(self):
		with self.assertRaisesRegex(frappe.ValidationError, "PINV-2"):
			self.validate(self.make_disbursement(("PINV-2", 1000)), {"PINV-2": 0})


class TestSalarySlipJournalEntryLink(FrappeTestCase):
	def make_salary_row(self, **values):
		disbursement = frappe.get_doc(
			{
				"doctype": "Payment Disbursement",
				"name": "PAY-DIS-TEST",
				"company": "Wind Power LLC",
				"items": [
					{
						"reference_doctype": "Salary Slip",
						"reference_name": "SAL-0001",
						"amount": 450000,
						"snippe_reference": "po_ref",
						**values,
					}
				],
			}
		)
		return disbursement, disbursement.items[0]

	def test_booking_links_the_journal_entry_on_the_slip(self):
		disbursement, row = self.make_salary_row()
		with (
			patch.object(
				disbursement_entries, "get_payable_line", return_value={"debit_in_account_currency": 1}
			),
			patch.object(
				disbursement_entries, "make_journal_entry", return_value=frappe._dict(name="ACC-JV-1")
			),
			patch.object(disbursement_entries.frappe.db, "set_value") as set_value,
		):
			disbursement_entries.book_completed_payout(disbursement, row)
		set_value.assert_called_once_with("Salary Slip", "SAL-0001", "journal_entry", "ACC-JV-1")

	def test_reversal_clears_the_journal_entry_on_the_slip(self):
		disbursement, row = self.make_salary_row(
			accounting_status="Booked", accounting_entry_type="Journal Entry", accounting_entry="ACC-JV-1"
		)
		with (
			patch.object(disbursement_entries, "reverse_journal_entry", return_value={}),
			patch.object(disbursement_entries.frappe.db, "set_value") as set_value,
		):
			disbursement_entries.reverse_payout_booking(disbursement, row)
		set_value.assert_called_once_with("Salary Slip", "SAL-0001", "journal_entry", None)
