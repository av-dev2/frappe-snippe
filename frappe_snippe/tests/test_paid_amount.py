from unittest.mock import MagicMock, patch

import frappe
from frappe.model.document import Document
from frappe.tests.utils import FrappeTestCase

from frappe_snippe.api import webhooks
from frappe_snippe.tests.test_collection_customer import make_collection, make_customer


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


@patch.object(Document, "db_set")
class TestPaymentEntryAmount(FrappeTestCase):
	def post(self, collection, paid_amount):
		payment_entry = MagicMock(name="PE-0001")
		payment_entry.name = "PE-0001"
		with (
			patch.object(webhooks.frappe, "new_doc", return_value=payment_entry),
			patch.object(
				webhooks.frappe, "get_single", return_value=frappe._dict(snippe_bank_account="Snippe - TC")
			),
		):
			webhooks.create_payment_entry(collection, paid_amount)
		return payment_entry

	def test_payment_entry_posts_amount_actually_paid(self, db_set):
		collection = make_collection_with_references(100000, 50000)
		payment_entry = self.post(collection, 120000)

		self.assertEqual((payment_entry.paid_amount, payment_entry.received_amount), (120000, 120000))
		references = [call.args[1] for call in payment_entry.append.call_args_list]
		self.assertEqual(
			[(row["reference_name"], row["allocated_amount"]) for row in references],
			[("SINV-0001", 100000), ("SINV-0002", 20000)],
		)
		payment_entry.submit.assert_called_once()

	def test_only_fully_covered_references_are_marked_paid(self, db_set):
		collection = make_collection_with_references(100000, 50000, 30000)
		self.post(collection, 120000)
		self.assertEqual(
			[call.args[0] for call in db_set.call_args_list],
			[
				{"payment_entry": "PE-0001", "status": "Paid"},
				{"payment_entry": "PE-0001", "status": "Pending"},
			],
		)


class TestCustomAmountCollection(FrappeTestCase):
	def setUp(self):
		self.customer = make_customer()

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
