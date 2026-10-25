from unittest.mock import patch

import frappe
from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
from frappe.model.document import Document

from frappe_snippe.accounting.amounts_due import get_amounts_left_to_pay
from frappe_snippe.accounting.disbursement_entries import book_completed_payout
from frappe_snippe.tests.test_collection_customer import make_customer
from frappe_snippe.tests.test_paid_amount import (
	SERVICE_ITEM,
	TEST_SUPPLIER,
	make_purchase_invoice,
	make_sales_invoice,
	submit_invoice,
)
from frappe_snippe.tests.test_snippe_setting import (
	TZS_CASH,
	TZS_COMPANY,
	TZS_FEES,
	TzsCompanyTestCase,
	make_snippe_setting,
)


def make_disbursement(reference_doctype, reference_name, amount, **values):
	return frappe.get_doc(
		{
			"doctype": "Payment Disbursement",
			"name": "PAY-DIS-FLOW",
			"company": TZS_COMPANY,
			"posting_date": frappe.utils.nowdate(),
			"paid_from_account": TZS_CASH,
			"items": [
				{
					"name": "row-1",
					"idx": 1,
					"reference_doctype": reference_doctype,
					"reference_name": reference_name,
					"amount": amount,
					"status": "Completed",
					"snippe_reference": "po_flow",
					"fee_amount": 1500,
				}
			],
			**values,
		}
	)


def get_gl_lines(journal_entry):
	return {
		(entry.account, entry.debit, entry.credit)
		for entry in frappe.get_all(
			"GL Entry",
			filters={"voucher_no": journal_entry, "is_cancelled": 0},
			fields=["account", "debit", "credit"],
		)
	}


class TestRefundsAndSupplierAdvances(TzsCompanyTestCase):
	def setUp(self):
		make_snippe_setting(auto_submit_accounting_entries=1)

	def test_credit_note_refund_pays_the_customer_back(self):
		invoice = make_sales_invoice(make_customer().name, 20000)
		credit_note = make_sales_return(invoice.name)
		credit_note.insert()
		credit_note.submit()
		self.assertEqual(
			get_amounts_left_to_pay("Sales Invoice", [credit_note.name, invoice.name], ""),
			{credit_note.name: 20000, invoice.name: 0},
		)

		disbursement = make_disbursement("Sales Invoice", credit_note.name, 20000)
		values = book_completed_payout(disbursement, disbursement.items[0])

		self.assertEqual(
			get_gl_lines(values["accounting_entry"]),
			{("Debtors - STZ", 20000, 0), (TZS_FEES, 1500, 0), (TZS_CASH, 0, 21500)},
		)
		self.assertEqual(frappe.db.get_value("Sales Invoice", credit_note.name, "outstanding_amount"), 0)

	def test_supplier_advance_is_booked_against_the_purchase_order(self):
		make_purchase_invoice(1)  # creates the test supplier and service item
		order = submit_invoice(
			{
				"doctype": "Purchase Order",
				"company": TZS_COMPANY,
				"supplier": TEST_SUPPLIER,
				"currency": "TZS",
				"buying_price_list": None,
				"schedule_date": frappe.utils.nowdate(),
				"items": [{"item_code": SERVICE_ITEM, "qty": 1, "rate": 30000}],
			}
		)
		self.assertEqual(get_amounts_left_to_pay("Purchase Order", [order.name], ""), {order.name: 30000})

		disbursement = make_disbursement("Purchase Order", order.name, 30000)
		values = book_completed_payout(disbursement, disbursement.items[0])

		journal_entry = frappe.get_doc("Journal Entry", values["accounting_entry"])
		payable = next(row for row in journal_entry.accounts if row.party)
		self.assertEqual(
			(payable.party, payable.reference_type, payable.is_advance, payable.debit),
			(TEST_SUPPLIER, "Purchase Order", "Yes", 30000),
		)
		self.assertEqual(frappe.db.get_value("Purchase Order", order.name, "advance_paid"), 30000)


@patch.object(Document, "insert", autospec=True, side_effect=lambda doc: doc)
class TestResendFailedRows(TzsCompanyTestCase):
	def make_submitted_disbursement(self):
		disbursement = frappe.get_doc(
			{
				"doctype": "Payment Disbursement",
				"name": "PAY-DIS-DONE",
				"company": TZS_COMPANY,
				"disbursement_type": "Purchase Invoice",
				"narration": "Supplier payments",
				"docstatus": 1,
				"items": [
					{"reference_name": "PINV-1", "amount": 1000, "status": "Completed"},
					{"reference_name": "PINV-2", "amount": 2000, "status": "Failed", "phone_number": "0712"},
					{"reference_name": "PINV-3", "amount": 3000, "status": "Reversed", "bank_code": "CRDB"},
				],
			}
		)
		for row in disbursement.items:
			row.update(
				{"reference_doctype": "Purchase Invoice", "channel": "Mobile Money", "snippe_reference": "po"}
			)
		return disbursement

	def test_failed_and_reversed_rows_go_to_a_new_draft(self, insert):
		disbursement = self.make_submitted_disbursement()
		with patch.object(frappe, "get_doc", wraps=frappe.get_doc) as get_doc:
			disbursement.make_retry_disbursement()

		retry = get_doc.call_args.args[0]
		self.assertEqual((retry["retry_of"], retry["narration"]), ("PAY-DIS-DONE", "Supplier payments"))
		self.assertEqual(
			[
				(row["reference_name"], row["amount"], row["phone_number"], row["bank_code"])
				for row in retry["items"]
			],
			[("PINV-2", 2000, "0712", None), ("PINV-3", 3000, None, "CRDB")],
		)
		self.assertNotIn("snippe_reference", retry["items"][0])

	def test_disbursement_without_failed_rows_cannot_be_resent(self, insert):
		disbursement = self.make_submitted_disbursement()
		for row in disbursement.items:
			row.status = "Completed"
		with self.assertRaisesRegex(frappe.ValidationError, "failed or reversed rows"):
			disbursement.make_retry_disbursement()
