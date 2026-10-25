import frappe
from erpnext.accounts.utils import get_balance_on
from frappe.tests.utils import FrappeTestCase

from frappe_snippe.accounting.disbursement_entries import book_completed_payout
from frappe_snippe.api import webhooks
from frappe_snippe.tests.test_collection_customer import make_collection, make_customer
from frappe_snippe.tests.test_snippe_setting import (
	TZS_CASH,
	TZS_COMPANY,
	TZS_FEES,
	TzsCompanyTestCase,
	make_snippe_setting,
)

SERVICE_ITEM = "Snippe Test Service"
TEST_SUPPLIER = "Snippe Test Supplier"


def make_collection_with_references(*amounts, **values):
	collection = frappe.get_doc(
		{
			"doctype": "Payment Collection",
			"name": "PAY-COL-TEST",
			"company": "Test Company",
			"customer": "CUST-0001",
			"posting_date": "2026-10-06",
			"snippe_reference": "sess_abc123",
			**values,
		}
	)
	for index, amount in enumerate(amounts, start=1):
		collection.append(
			"references",
			{
				"name": f"row-{index}",
				"reference_doctype": "Sales Invoice",
				"reference_name": f"SINV-000{index}",
				"allocated_amount": amount,
			},
		)
	return collection


def allocated(collection, paid_amount):
	return [(row.reference_name, amount) for row, amount in collection.get_reference_allocations(paid_amount)]


class TestReferenceAllocations(FrappeTestCase):
	def test_full_payment_covers_every_reference(self):
		collection = make_collection_with_references(1000, 2000, 3000)
		self.assertEqual(
			allocated(collection, 6000), [("SINV-0001", 1000), ("SINV-0002", 2000), ("SINV-0003", 3000)]
		)

	def test_short_payment_fills_references_in_row_order(self):
		collection = make_collection_with_references(1000, 2000, 3000)
		self.assertEqual(allocated(collection, 2500), [("SINV-0001", 1000), ("SINV-0002", 1500)])

	def test_overpayment_leaves_excess_unallocated(self):
		collection = make_collection_with_references(1000, 2000)
		self.assertEqual(allocated(collection, 5000), [("SINV-0001", 1000), ("SINV-0002", 2000)])

	def test_decimal_amounts_do_not_drift(self):
		collection = make_collection_with_references(0.1, 0.2, 0.3)
		self.assertEqual(
			allocated(collection, 0.6), [("SINV-0001", 0.1), ("SINV-0002", 0.2), ("SINV-0003", 0.3)]
		)


def make_sales_invoice(customer, rate):
	return submit_invoice(
		{
			"doctype": "Sales Invoice",
			"company": TZS_COMPANY,
			"customer": customer,
			"currency": "TZS",
			"selling_price_list": None,
			"items": [{"item_code": SERVICE_ITEM, "qty": 1, "rate": rate}],
		}
	)


def make_purchase_invoice(rate):
	if not frappe.db.exists("Supplier", TEST_SUPPLIER):
		frappe.get_doc(
			{"doctype": "Supplier", "supplier_name": TEST_SUPPLIER, "supplier_group": "All Supplier Groups"}
		).insert()

	return submit_invoice(
		{
			"doctype": "Purchase Invoice",
			"company": TZS_COMPANY,
			"supplier": TEST_SUPPLIER,
			"currency": "TZS",
			"buying_price_list": None,
			"items": [{"item_code": SERVICE_ITEM, "qty": 1, "rate": rate}],
		}
	)


def submit_invoice(values):
	if not frappe.db.exists("Item", SERVICE_ITEM):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": SERVICE_ITEM,
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()

	invoice = frappe.get_doc(values)
	invoice.insert()
	invoice.submit()
	return invoice


def make_paid_collection(customer, *rates):
	"""A Payment Collection for one new Sales Invoice per rate."""
	return frappe.get_doc(
		{
			"doctype": "Payment Collection",
			"company": TZS_COMPANY,
			"customer": customer,
			"posting_date": frappe.utils.nowdate(),
			"payment_method": "Mobile Money",
			"phone_number": "0712345678",
			"snippe_reference": "pi_ref",
			"references": [
				{
					"reference_doctype": "Sales Invoice",
					"reference_name": make_sales_invoice(customer, rate).name,
					"allocated_amount": rate,
				}
				for rate in rates
			],
		}
	).insert()


class TestPaymentEntryBooking(TzsCompanyTestCase):
	"""Posts real Payment Entries against real Sales Invoices of the TZS test company."""

	def setUp(self):
		self.customer = make_customer().name
		self.setting = make_snippe_setting()

	def make_paid_collection(self, *rates):
		return make_paid_collection(self.customer, *rates)

	def get_gl_lines(self, payment_entry):
		return {
			(entry.account, entry.debit, entry.credit)
			for entry in frappe.get_all(
				"GL Entry",
				filters={"voucher_no": payment_entry, "is_cancelled": 0},
				fields=["account", "debit", "credit"],
			)
		}

	def test_snippe_fee_is_booked_and_invoice_is_fully_paid(self):
		collection = self.make_paid_collection(50000)
		webhooks.create_payment_entry(collection, 50000, 1000)

		row = collection.references[0]
		self.assertEqual(row.status, "Paid")
		self.assertEqual(
			self.get_gl_lines(row.payment_entry),
			{("Debtors - STZ", 0, 50000), (TZS_CASH, 49000, 0), (TZS_FEES, 1000, 0)},
		)
		self.assertEqual(frappe.db.get_value("Sales Invoice", row.reference_name, "outstanding_amount"), 0)

	def test_short_payment_fills_references_in_row_order(self):
		collection = self.make_paid_collection(100000, 50000, 30000)
		webhooks.create_payment_entry(collection, 120000)

		self.assertEqual(
			[(row.status, row.payment_entry is not None) for row in collection.references],
			[("Paid", True), ("Pending", True), ("Pending", False)],
		)
		outstanding = [
			frappe.db.get_value("Sales Invoice", row.reference_name, "outstanding_amount")
			for row in collection.references
		]
		self.assertEqual(outstanding, [0, 30000, 30000])

	def test_fee_without_fees_account_is_rejected(self):
		self.setting.db_set("fees_account", None)
		with self.assertRaisesRegex(frappe.ValidationError, "Fees Account"):
			webhooks.create_payment_entry(self.make_paid_collection(50000), 50000, 1000)

	def test_company_without_gl_account_is_rejected(self):
		self.setting.db_set("wallet_account", None)
		with self.assertRaisesRegex(frappe.ValidationError, "Wallet Account"):
			webhooks.create_payment_entry(self.make_paid_collection(50000), 50000)


class TestCustomAmountCollection(TzsCompanyTestCase):
	def setUp(self):
		self.customer = make_customer()
		make_snippe_setting()

	def test_custom_amount_limits_reach_the_session(self):
		collection = make_collection(self.customer, "Hosted Session")
		collection.update({"allow_custom_amount": 1, "min_amount": 1000.4, "max_amount": 500000})
		self.assertEqual(
			collection.get_custom_amount_limits(),
			{"allow_custom_amount": True, "min_amount": 1000, "max_amount": 500000},
		)

	def test_fixed_amount_session_sends_no_limits(self):
		self.assertEqual(make_collection(self.customer, "Hosted Session").get_custom_amount_limits(), {})

	def test_custom_amount_needs_hosted_session(self):
		collection = make_collection(self.customer, "Mobile Money")
		collection.allow_custom_amount = 1
		with self.assertRaisesRegex(frappe.ValidationError, "Hosted Session"):
			collection.validate()


class TestWalletAccountBalance(TzsCompanyTestCase):
	"""Collections add to the Wallet Account and payouts take from it, so it matches the Snippe balance."""

	def test_wallet_balance_is_collections_less_payouts_and_fees(self):
		make_snippe_setting(auto_submit_accounting_entries=1)
		webhooks.create_payment_entry(make_paid_collection(make_customer().name, 50000), 50000, 1000)

		purchase_invoice = make_purchase_invoice(30000)
		disbursement = frappe.get_doc(
			{
				"doctype": "Payment Disbursement",
				"company": TZS_COMPANY,
				"items": [
					{
						"reference_doctype": "Purchase Invoice",
						"reference_name": purchase_invoice.name,
						"recipient_name": TEST_SUPPLIER,
						"amount": 30000,
						"channel": "Mobile Money",
						"phone_number": "0712345678",
						"snippe_reference": "po_wallet",
						"status": "Completed",
						"fee_amount": 1500,
					}
				],
			}
		)
		disbursement.validate()
		book_completed_payout(disbursement, disbursement.items[0])

		self.assertEqual(get_balance_on(TZS_CASH, company=TZS_COMPANY), 50000 - 1000 - 30000 - 1500)
		self.assertEqual(
			frappe.db.get_value("Purchase Invoice", purchase_invoice.name, "outstanding_amount"), 0
		)
