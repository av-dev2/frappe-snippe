import sys
import types
from unittest.mock import MagicMock, patch

import frappe
from frappe.model.document import Document
from frappe.tests.utils import FrappeTestCase

from frappe_snippe import tasks
from frappe_snippe.accounting import disbursement_entries
from frappe_snippe.accounting.disbursement_entries import (
	book_completed_payout,
	get_payable_line,
	reverse_payout_booking,
)

COMPANY = "Wind Power LLC"
CASH = "Cash - WP"
FEES = "Administrative Expenses - WP"
LENDING_LOAN_MODULE = "lending.loan_management.doctype.loan.loan"


def set_in_memory(document, values, value=None):
	"""db_set stand-in for unsaved test rows."""
	if isinstance(values, dict):
		document.update(values)
	else:
		document.set(values, value)


def make_disbursement(**row_values):
	return frappe.get_doc(
		{
			"doctype": "Payment Disbursement",
			"name": "PAY-DIS-TEST",
			"company": COMPANY,
			"paid_from_account": CASH,
			"items": [
				{
					"name": "row-1",
					"idx": 1,
					"reference_doctype": "Purchase Invoice",
					"reference_name": "PINV-0001",
					"amount": 50000.4,
					"snippe_reference": "po_ref",
					"status": "Completed",
					**row_values,
				}
			],
		}
	)


def set_settings(auto_submit=1, fees_account=FEES):
	frappe.db.set_single_value(
		"Snippe Settings",
		{"auto_submit_accounting_entries": auto_submit, "snippe_fees_account": fees_account},
	)


class InMemoryRowTestCase(FrappeTestCase):
	"""Rows are never saved, so db_set only updates them in memory."""

	def setUp(self):
		patcher = patch.object(Document, "db_set", autospec=True, side_effect=set_in_memory)
		patcher.start()
		self.addCleanup(patcher.stop)


class TestJournalEntryPosting(InMemoryRowTestCase):
	"""Posts real Journal Entries, using a fee-only payout so no party documents are needed."""

	def book_fee_payout(self, auto_submit=1):
		set_settings(auto_submit)
		disbursement = make_disbursement(fee_amount=350)
		row = disbursement.items[0]
		journal_entry = disbursement_entries.make_journal_entry(
			disbursement, row, disbursement_entries.get_fee_lines(disbursement, row)
		)
		row.update(
			{
				"accounting_status": "Booked",
				"accounting_entry_type": "Journal Entry",
				"accounting_entry": journal_entry.name,
			}
		)
		return disbursement, row, journal_entry

	def reverse(self, disbursement, row):
		row.status = "Reversed"
		disbursement.book_item(row)

	def test_entry_credits_paid_from_account_and_carries_snippe_reference(self):
		_disbursement, _row, journal_entry = self.book_fee_payout()
		self.assertEqual(journal_entry.docstatus, 1)
		self.assertEqual((journal_entry.voucher_type, journal_entry.cheque_no), ("Bank Entry", "po_ref"))
		self.assertIn("PAY-DIS-TEST row 1", journal_entry.user_remark)
		lines = {line.account: (line.debit, line.credit) for line in journal_entry.accounts}
		self.assertEqual(lines, {FEES: (350, 0), CASH: (0, 350)})

	def test_entries_stay_draft_without_auto_submit(self):
		_disbursement, _row, journal_entry = self.book_fee_payout(auto_submit=0)
		self.assertEqual(journal_entry.docstatus, 0)

	def test_reversal_posts_mirror_entry_dated_today(self):
		disbursement, row, journal_entry = self.book_fee_payout()
		self.reverse(disbursement, row)

		reversal = frappe.get_doc("Journal Entry", row.reversal_journal_entry)
		self.assertEqual(row.accounting_status, "Reversed")
		self.assertEqual((reversal.docstatus, reversal.reversal_of), (1, journal_entry.name))
		self.assertEqual((str(reversal.posting_date), reversal.cheque_no), (frappe.utils.nowdate(), "po_ref"))
		lines = {line.account: (line.debit, line.credit) for line in reversal.accounts}
		self.assertEqual(lines, {FEES: (0, 350), CASH: (350, 0)})

	def test_repeated_reversal_does_nothing(self):
		disbursement, row, _journal_entry = self.book_fee_payout()
		self.reverse(disbursement, row)
		first_reversal = row.reversal_journal_entry
		self.assertIsNone(reverse_payout_booking(disbursement, row))
		self.assertEqual(row.reversal_journal_entry, first_reversal)

	def test_reversing_a_draft_deletes_it(self):
		disbursement, row, journal_entry = self.book_fee_payout(auto_submit=0)
		self.reverse(disbursement, row)
		self.assertFalse(frappe.db.exists("Journal Entry", journal_entry.name))
		self.assertEqual((row.accounting_entry, row.accounting_status), (None, "Reversed"))


class TestPayableLines(FrappeTestCase):
	def line_for(self, reference_doctype, values):
		row = make_disbursement(reference_doctype=reference_doctype, reference_name="REF-1").items[0]
		with patch.object(disbursement_entries.frappe.db, "get_value", side_effect=values):
			return get_payable_line(row)

	def test_purchase_invoice_pays_the_supplier_payable(self):
		line = self.line_for("Purchase Invoice", [("Creditors - WP", "SUP-1")])
		self.assertEqual(
			line,
			{
				"account": "Creditors - WP",
				"party_type": "Supplier",
				"party": "SUP-1",
				"reference_type": "Purchase Invoice",
				"reference_name": "REF-1",
				"debit_in_account_currency": 50000,
			},
		)

	def test_expense_claim_and_advance_pay_the_employee(self):
		claim = self.line_for("Expense Claim", [("Employee Payable - WP", "EMP-1")])
		advance = self.line_for("Employee Advance", [("Employee Advances - WP", "EMP-1")])
		self.assertEqual(
			(claim["account"], claim["party_type"], claim["reference_type"], claim.get("is_advance")),
			("Employee Payable - WP", "Employee", "Expense Claim", None),
		)
		self.assertEqual(
			(advance["account"], advance["party"], advance["reference_type"], advance["is_advance"]),
			("Employee Advances - WP", "EMP-1", "Employee Advance", "Yes"),
		)

	def test_salary_slip_pays_payroll_payable_against_payroll_entry(self):
		line = self.line_for("Salary Slip", [("EMP-1", "HR-PRUN-0001"), "Payroll Payable - WP"])
		self.assertEqual(
			(line["account"], line["party"], line["reference_type"], line["reference_name"]),
			("Payroll Payable - WP", "EMP-1", "Payroll Entry", "HR-PRUN-0001"),
		)

	def test_salary_slip_without_payroll_entry_is_rejected(self):
		with self.assertRaisesRegex(frappe.ValidationError, "no Payroll Entry"):
			self.line_for("Salary Slip", [("EMP-1", None)])


class TestCompletedPayoutBooking(FrappeTestCase):
	def test_journal_entry_gets_payable_and_fee_lines(self):
		set_settings()
		disbursement = make_disbursement(fee_amount=350)
		payable = {"account": "Creditors - WP", "debit_in_account_currency": 50000}
		with (
			patch.object(disbursement_entries, "get_payable_line", return_value=payable),
			patch.object(
				disbursement_entries, "make_journal_entry", return_value=frappe._dict(name="JE-1")
			) as make,
		):
			values = book_completed_payout(disbursement, disbursement.items[0])

		self.assertEqual([line["account"] for line in make.call_args.args[2]], ["Creditors - WP", FEES])
		self.assertEqual(
			values,
			{
				"accounting_status": "Booked",
				"accounting_entry_type": "Journal Entry",
				"accounting_entry": "JE-1",
			},
		)

	def test_fee_without_fees_account_is_rejected(self):
		set_settings(fees_account=None)
		disbursement = make_disbursement(fee_amount=350)
		with self.assertRaisesRegex(frappe.ValidationError, "Snippe Fees Account"):
			disbursement_entries.get_fee_lines(disbursement, disbursement.items[0])

	def test_already_booked_row_is_not_booked_again(self):
		disbursement = make_disbursement(accounting_status="Booked")
		with patch.object(disbursement_entries, "make_journal_entry") as make:
			self.assertIsNone(book_completed_payout(disbursement, disbursement.items[0]))
		make.assert_not_called()

	def test_loan_payout_creates_lending_loan_disbursement_and_fee_entry(self):
		set_settings()
		disbursement = make_disbursement(reference_doctype="Loan", reference_name="LOAN-1", fee_amount=350)
		loan_entry = MagicMock()
		loan_entry.name = "LD-1"
		lending_loan = types.ModuleType(LENDING_LOAN_MODULE)
		lending_loan.make_loan_disbursement = MagicMock(return_value=loan_entry)
		loan = frappe._dict(company=COMPANY, applicant_type="Employee", applicant="EMP-1")
		with (
			patch.dict(sys.modules, {LENDING_LOAN_MODULE: lending_loan}),
			patch.object(disbursement_entries.frappe.db, "get_value", return_value=loan),
			patch.object(disbursement_entries, "save_entry", side_effect=lambda entry: entry),
			patch.object(
				disbursement_entries, "make_journal_entry", return_value=frappe._dict(name="JE-FEE")
			) as make,
		):
			values = book_completed_payout(disbursement, disbursement.items[0])

		lending_loan.make_loan_disbursement.assert_called_once_with(
			"LOAN-1", COMPANY, "Employee", "EMP-1", pending_amount=50000
		)
		loan_entry.update.assert_called_once_with(
			{
				"disbursement_account": CASH,
				"reference_number": "po_ref",
				"reference_date": frappe.utils.nowdate(),
			}
		)
		self.assertEqual([line["account"] for line in make.call_args.args[2]], [FEES])
		self.assertEqual(
			values,
			{
				"accounting_status": "Booked",
				"accounting_entry_type": "Loan Disbursement",
				"accounting_entry": "LD-1",
				"fee_journal_entry": "JE-FEE",
			},
		)

	def test_reversed_loan_payout_cancels_loan_disbursement(self):
		disbursement = make_disbursement(
			reference_doctype="Loan",
			accounting_status="Booked",
			accounting_entry_type="Loan Disbursement",
			accounting_entry="LD-1",
		)
		loan_entry = MagicMock(docstatus=1)
		with patch.object(disbursement_entries.frappe, "get_doc", return_value=loan_entry):
			values = reverse_payout_booking(disbursement, disbursement.items[0])
		loan_entry.cancel.assert_called_once()
		self.assertEqual(values, {"accounting_status": "Reversed"})


class TestPayoutStatusBooking(InMemoryRowTestCase):
	def test_status_change_books_once_and_failures_are_logged(self):
		disbursement = make_disbursement(status="Pending")
		row = disbursement.items[0]
		with (
			patch.object(
				disbursement_entries, "get_payable_line", side_effect=frappe.ValidationError("boom")
			),
			patch.object(frappe, "log_error") as log_error,
		):
			disbursement.set_item_status(row, "Completed")
			disbursement.set_item_status(row, "Completed")

		self.assertEqual((row.status, row.accounting_status), ("Completed", None))
		log_error.assert_called_once()
		self.assertEqual(log_error.call_args.kwargs["reference_name"], "PAY-DIS-TEST")

	def test_change_already_recorded_by_another_job_is_skipped(self):
		disbursement = make_disbursement(status="Pending")
		with (
			patch.object(frappe.db, "get_value", return_value="Completed") as get_value,
			patch.object(disbursement, "book_item") as book_item,
		):
			disbursement.set_item_status(disbursement.items[0], "Completed")

		get_value.assert_called_once_with("Payment Disbursement Item", "row-1", "status", for_update=True)
		self.assertEqual(disbursement.items[0].status, "Pending")
		book_item.assert_not_called()


class TestAccountingRetry(FrappeTestCase):
	def test_hourly_job_loads_each_disbursement_once(self):
		disbursement = MagicMock()
		disbursement.items = [
			frappe._dict(name="row-1"),
			frappe._dict(name="row-2"),
			frappe._dict(name="row-3"),
		]
		with (
			patch.object(
				tasks.frappe,
				"get_all",
				side_effect=[
					[frappe._dict(parent="PD-1", name="row-1")],
					[frappe._dict(parent="PD-1", name="row-3")],
				],
			) as get_all,
			patch.object(tasks.frappe, "get_doc", return_value=disbursement) as get_doc,
		):
			tasks.book_missing_payout_accounting()

		self.assertEqual(
			[call.kwargs["filters"] for call in get_all.call_args_list],
			[dict(f) for f in tasks.RETRY_FILTERS],
		)
		get_doc.assert_called_once_with("Payment Disbursement", "PD-1", for_update=True)
		self.assertEqual(
			[call.args[0].name for call in disbursement.book_item.call_args_list], ["row-1", "row-3"]
		)


class TestFeesAccountSetting(FrappeTestCase):
	def test_fees_account_must_be_a_ledger_expense_account(self):
		for account in (CASH, "Indirect Expenses - WP"):
			settings = frappe.get_doc("Snippe Settings")
			settings.snippe_fees_account = account
			with self.assertRaises(frappe.ValidationError, msg=account):
				settings.save()
		settings = frappe.get_doc("Snippe Settings")
		settings.snippe_fees_account = FEES
		settings.save()
