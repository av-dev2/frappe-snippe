from unittest.mock import MagicMock, patch

import frappe
from snippe import Payment

from frappe_snippe.api import webhooks
from frappe_snippe.frappe_snippe.doctype.payment_collection import payment_collection
from frappe_snippe.tests.test_collection_customer import make_customer
from frappe_snippe.tests.test_paid_amount import (
	SERVICE_ITEM,
	insert_invoice,
	make_sales_invoice,
)
from frappe_snippe.tests.test_snippe_setting import (
	PHONE_MODE,
	TZS_CASH,
	TZS_COMPANY,
	TZS_FEES,
	TzsCompanyTestCase,
	make_phone_mode_of_payment,
	make_snippe_setting,
)
from frappe_snippe.tests.test_snippe_setting import (
	TZS_ABBR as TZS_COMPANY_ABBR,
)


def make_draft_sales_invoice(customer, rate):
	return insert_invoice(
		{
			"doctype": "Sales Invoice",
			"company": TZS_COMPANY,
			"customer": customer,
			"currency": "TZS",
			"selling_price_list": None,
			"items": [{"item_code": SERVICE_ITEM, "qty": 1, "rate": rate}],
		}
	)


def make_client():
	client = MagicMock()
	client.__enter__.return_value = client
	client.create_mobile_payment.return_value = Payment.from_dict(
		{"reference": "pi_push", "status": "pending", "amount": {"value": 0, "currency": "TZS"}}
	)
	return client


class InvoicePaymentTestCase(TzsCompanyTestCase):
	def setUp(self):
		make_snippe_setting()
		make_phone_mode_of_payment()
		self.customer = make_customer().name

	def make_collection(self, **values):
		return frappe.get_doc(
			{
				"doctype": "Payment Collection",
				"company": TZS_COMPANY,
				"customer": self.customer,
				"payment_method": "Mobile Money",
				"mode_of_payment": PHONE_MODE,
				"references": [
					{
						"reference_doctype": "Sales Invoice",
						"reference_name": make_sales_invoice(self.customer, 5000).name,
						"allocated_amount": 5000,
					}
				],
				**values,
			}
		)

	def pay(self, invoice, amount, payment_row=None):
		with patch.object(payment_collection, "get_snippe_client", return_value=make_client()):
			name = payment_collection.make_payment(
				invoice.doctype, invoice.name, "0712345678", PHONE_MODE, amount, payment_row
			)
		return frappe.get_doc("Payment Collection", name)


class TestCollectionRules(InvoicePaymentTestCase):
	def test_phone_comes_from_the_customer(self):
		frappe.db.set_value("Customer", self.customer, "mobile_no", "0754000111")
		collection = self.make_collection()
		collection.validate()
		self.assertEqual(collection.phone_number, "255754000111")

	def test_missing_phone_blocks_only_submit(self):
		collection = self.make_collection()
		collection.insert()
		with self.assertRaisesRegex(frappe.ValidationError, "Phone Number is required"):
			collection.submit()

	def test_mode_of_payment_must_be_of_type_phone(self):
		with self.assertRaisesRegex(frappe.ValidationError, "must be of type Phone"):
			self.make_collection(mode_of_payment="Cash").validate()

	def test_mode_of_payment_may_post_to_its_own_account(self):
		make_phone_mode_of_payment(account=make_sales_team_account())
		self.make_collection().validate()

	def test_draft_invoice_must_be_the_only_reference(self):
		collection = self.make_collection()
		collection.append(
			"references",
			{
				"reference_doctype": "Sales Invoice",
				"reference_name": make_draft_sales_invoice(self.customer, 3000).name,
				"allocated_amount": 3000,
			},
		)
		with self.assertRaisesRegex(frappe.ValidationError, "only reference"):
			collection.validate()

	def test_only_a_draft_pos_invoice_can_be_paid(self):
		collection = self.make_collection()
		collection.references[0].update(
			{"reference_doctype": "POS Invoice", "reference_name": "POS-SUBMITTED"}
		)
		with (
			patch.object(payment_collection, "validate_rows_currency"),
			self.assertRaisesRegex(frappe.ValidationError, "Only a draft POS Invoice"),
		):
			collection.validate()


class TestPayInvoice(InvoicePaymentTestCase):
	def test_dialog_defaults_come_from_the_invoice(self):
		frappe.db.set_value("Customer", self.customer, "mobile_no", "0754000111")
		invoice = make_draft_sales_invoice(self.customer, 20000)
		defaults = payment_collection.get_payment_defaults("Sales Invoice", invoice.name)
		self.assertEqual(defaults, {"phone_number": "0754000111", "mode_of_payment": None, "amount": 20000})

	def test_push_creates_and_submits_a_collection_for_the_draft(self):
		invoice = make_draft_sales_invoice(self.customer, 20000)
		collection = self.pay(invoice, 20000)

		self.assertEqual((collection.docstatus, collection.status), (1, "Pending"))
		self.assertEqual(collection.phone_number, "255712345678")
		self.assertEqual(
			(collection.references[0].reference_name, collection.references[0].allocated_amount),
			(invoice.name, 20000),
		)

	def test_amount_cannot_exceed_what_is_left(self):
		invoice = make_draft_sales_invoice(self.customer, 20000)
		with self.assertRaisesRegex(frappe.ValidationError, "at most 20000"):
			self.pay(invoice, 25000)

	def test_submitted_invoice_pays_its_outstanding_amount(self):
		invoice = make_sales_invoice(self.customer, 8000)
		self.assertEqual(payment_collection.get_amount_left(invoice), 8000)
		with self.assertRaisesRegex(frappe.ValidationError, "at most 8000"):
			self.pay(invoice, 9000)

	def test_only_invoices_can_be_paid(self):
		with self.assertRaisesRegex(
			frappe.ValidationError, "only for a Sales Invoice, POS Invoice, Loan or Customer"
		):
			payment_collection.make_payment("Journal Entry", "JV-1", "0712345678", PHONE_MODE, 500)

	def test_pos_amount_left_excludes_other_payment_rows(self):
		invoice = frappe._dict(
			docstatus=0,
			is_pos=1,
			rounded_total=30000,
			payments=[frappe._dict(name="cash", amount=10000), frappe._dict(name="phone", amount=0)],
		)
		self.assertEqual(payment_collection.get_amount_left(invoice, "phone"), 20000)


class TestBookCollection(InvoicePaymentTestCase):
	def test_draft_invoice_gets_the_payment_as_an_advance(self):
		invoice = make_draft_sales_invoice(self.customer, 20000)
		collection = self.pay(invoice, 20000)
		webhooks.book_collection(collection, 20000, 500)

		invoice.reload()
		advance = invoice.advances[0]
		payment_entry = frappe.get_doc("Payment Entry", advance.reference_name)
		self.assertEqual(
			(payment_entry.mode_of_payment, payment_entry.unallocated_amount), (PHONE_MODE, 20000)
		)
		self.assertEqual(advance.allocated_amount, 20000)

		invoice.submit()
		self.assertEqual(invoice.outstanding_amount, 0)

	def test_payment_after_the_invoice_was_settled_becomes_an_advance(self):
		invoice = make_sales_invoice(self.customer, 8000)
		collection = self.pay(invoice, 8000)
		frappe.db.set_value("Sales Invoice", invoice.name, "outstanding_amount", 0)
		with patch.object(webhooks, "notify_owner") as notify_owner:
			webhooks.book_collection(collection, 8000, 0)

		payment_entry = frappe.get_doc("Payment Entry", collection.references[0].payment_entry)
		self.assertEqual((payment_entry.unallocated_amount, len(payment_entry.references)), (8000, 0))
		self.assertIn("booked as advance", notify_owner.call_args.args[1])

	def book_pos_payment(self, paid_amount, fee_amount):
		invoice = FakePosInvoice(
			[
				frappe._dict(name="cash", mode_of_payment="Cash", amount=10000, reference_no=None),
				frappe._dict(name="phone", mode_of_payment=PHONE_MODE, amount=0, reference_no=None),
			]
		)
		collection = MagicMock(
			company=TZS_COMPANY,
			posting_date=frappe.utils.nowdate(),
			snippe_reference="pi_pos",
			mode_of_payment=PHONE_MODE,
			payment_row=None,
			references=[MagicMock()],
		)
		collection.name = "PAY-COL-POS"
		webhooks.add_pos_payment(invoice, collection, paid_amount, fee_amount)
		return invoice, collection

	def get_wallet_entry_lines(self, collection):
		field, journal_entry = collection.db_set.call_args.args
		self.assertEqual(field, "wallet_journal_entry")
		return {
			(row.account, row.debit, row.credit)
			for row in frappe.get_doc("Journal Entry", journal_entry).accounts
		}

	def test_pos_payment_through_the_wallet_books_only_the_fee(self):
		invoice, collection = self.book_pos_payment(20000, 400)

		self.assertEqual((invoice.payments[1].amount, invoice.payments[1].reference_no), (20000, "pi_pos"))
		self.assertTrue(invoice.saved)
		self.assertEqual(self.get_wallet_entry_lines(collection), {(TZS_FEES, 400, 0), (TZS_CASH, 0, 400)})

	def test_pos_payment_through_a_sales_team_account_moves_into_the_wallet(self):
		team_account = make_sales_team_account()
		make_phone_mode_of_payment(account=team_account)
		_invoice, collection = self.book_pos_payment(20000, 400)

		self.assertEqual(
			self.get_wallet_entry_lines(collection),
			{(team_account, 0, 20000), (TZS_CASH, 19600, 0), (TZS_FEES, 400, 0)},
		)

	def test_pos_payment_through_the_wallet_without_fee_needs_no_entry(self):
		_invoice, collection = self.book_pos_payment(20000, 0)
		collection.db_set.assert_not_called()

	def test_pos_payment_without_a_matching_row_adds_one(self):
		invoice = FakePosInvoice(
			[frappe._dict(name="cash", mode_of_payment="Cash", amount=10000, reference_no=None)]
		)
		collection = MagicMock(
			mode_of_payment=PHONE_MODE, payment_row=None, snippe_reference="pi_new", references=[]
		)
		with (
			patch.object(webhooks, "get_pos_wallet_amounts", return_value={}),
			patch.object(webhooks, "make_wallet_journal_entry", return_value=None),
		):
			webhooks.add_pos_payment(invoice, collection, 5000, 0)
		self.assertEqual(
			(invoice.payments[-1].mode_of_payment, invoice.payments[-1].amount), (PHONE_MODE, 5000)
		)


def make_sales_team_account():
	"""A TZS Cash ledger next to the Wallet Account, as a sales team's own Phone mode might use."""
	name = f"Sales Team Phone - {TZS_COMPANY_ABBR}"
	if not frappe.db.exists("Account", name):
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "Sales Team Phone",
				"company": TZS_COMPANY,
				"parent_account": frappe.db.get_value("Account", TZS_CASH, "parent_account"),
				"account_type": "Cash",
				"account_currency": "TZS",
			}
		).insert()
	return name


class FakePosInvoice:
	"""Draft POS invoice stand-in; a real one needs a POS Profile and stock setup."""

	is_pos = 1

	def __init__(self, payments):
		self.payments = payments
		self.saved = False

	def append(self, fieldname, values):
		row = frappe._dict(values)
		self.payments.append(row)
		return row

	def save(self, ignore_permissions=False):
		self.saved = True
