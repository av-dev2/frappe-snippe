from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from snippe import Balance, PayoutFee, RateLimitError

from frappe_snippe.frappe_snippe.doctype.payment_disbursement import payment_disbursement
from frappe_snippe.frappe_snippe.doctype.snippe_settings import snippe_settings


def make_disbursement(*amounts):
	return frappe.get_doc(
		{
			"doctype": "Payment Disbursement",
			"name": "PAY-DIS-TEST",
			"items": [
				{
					"name": f"row-{index}",
					"idx": index,
					"amount": amount,
					"channel": "Mobile Money",
					"phone_number": "255712345678",
				}
				for index, amount in enumerate(amounts, start=1)
			],
		}
	)


def make_checked_disbursement(snippe_balance):
	"""A 50,000 + 20,000 disbursement whose fees (500 + 200) and balance were already checked."""
	disbursement = make_disbursement(50000, 20000)
	disbursement.update(
		{
			"company": "Wind Power LLC",
			"snippe_balance": snippe_balance,
			"balance_checked_on": frappe.utils.now_datetime(),
		}
	)
	disbursement.items[0].fee_amount = 500
	disbursement.items[1].fee_amount = 200
	disbursement.calculate_totals()
	return disbursement


def make_client(available_balance=1_000_000):
	client = MagicMock()
	client.__enter__.return_value = client
	client.calculate_payout_fee.side_effect = lambda amount: PayoutFee(
		amount=amount, fee_amount=amount // 100, total_amount=amount + amount // 100, currency="TZS"
	)
	client.get_balance.return_value = Balance(
		available_balance=available_balance, balance=available_balance + 5000, currency="TZS"
	)
	return client


class TestCheckFeesAndBalance(FrappeTestCase):
	def check(self, disbursement, client):
		with (
			patch.object(payment_disbursement, "get_snippe_client", return_value=client),
			patch.object(disbursement, "check_permission"),
			patch.object(disbursement, "save") as save,
		):
			preview = disbursement.check_fees_and_balance()
		save.assert_called_once()
		return preview

	def test_check_saves_fees_totals_and_balance_on_the_draft(self):
		disbursement = make_disbursement(50000, 20000.4, 50000)
		client = make_client(available_balance=250000)
		self.check(disbursement, client)

		self.assertEqual([row.fee_amount for row in disbursement.items], [500, 200, 500])
		self.assertEqual(client.calculate_payout_fee.call_count, 2)
		self.assertEqual((disbursement.total_fee, disbursement.total_to_deduct), (1200, 121200.4))
		self.assertEqual(disbursement.snippe_balance, 250000)
		self.assertIsNotNone(disbursement.balance_checked_on)

	def test_check_returns_preview_for_the_dialog(self):
		disbursement = make_disbursement(50000, 20000)
		disbursement.items[0].recipient_name = "Asha Juma"
		preview = self.check(disbursement, make_client(available_balance=70000))

		self.assertEqual(
			preview["rows"][0], {"idx": 1, "recipient_name": "Asha Juma", "amount": 50000, "fee_amount": 500}
		)
		self.assertEqual(
			(preview["total_fee"], preview["total_to_deduct"], preview["available_balance"]),
			(700, 70700, 70000),
		)
		self.assertFalse(preview["is_balance_enough"])

	def test_snippe_errors_stop_the_check_with_a_message(self):
		client = make_client()
		client.calculate_payout_fee.side_effect = RateLimitError("Too many requests", 429)
		with self.assertRaisesRegex(frappe.ValidationError, "Too many requests"):
			self.check(make_disbursement(50000), client)


class TestSubmitUsesSavedBalance(FrappeTestCase):
	def submit(self, disbursement):
		with patch.object(payment_disbursement, "get_snippe_client") as get_client:
			disbursement.before_submit()
		get_client.assert_not_called()

	def test_submit_without_check_is_blocked(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Check Fees & Balance"):
			self.submit(make_disbursement(50000))

	def test_saved_balance_below_total_to_deduct_blocks_submit(self):
		with self.assertRaisesRegex(frappe.ValidationError, "below the"):
			self.submit(make_checked_disbursement(70699))

	def test_saved_balance_covering_amount_and_fees_allows_submit(self):
		self.submit(make_checked_disbursement(70700))


class TestAmountChangesClearCheck(FrappeTestCase):
	def validate_edit(self, edit):
		saved = make_checked_disbursement(70700)
		edited = frappe.get_doc(saved.as_dict())
		edit(edited)
		with patch.object(edited, "get_doc_before_save", return_value=saved):
			edited.validate()
		return edited

	def test_changed_amount_clears_saved_fees_and_balance(self):
		edited = self.validate_edit(lambda doc: doc.items[0].set("amount", 60000))
		self.assertEqual((edited.balance_checked_on, edited.snippe_balance, edited.total_fee), (None, 0, 0))
		self.assertEqual(edited.total_to_deduct, 80000)

	def test_unchanged_amounts_keep_the_check(self):
		edited = self.validate_edit(lambda doc: None)
		self.assertIsNotNone(edited.balance_checked_on)
		self.assertEqual((edited.total_fee, edited.snippe_balance), (700, 70700))


class TestSettingsBalance(FrappeTestCase):
	def test_balance_is_returned_without_being_saved(self):
		settings = frappe.get_doc("Snippe Settings")
		with patch.object(snippe_settings, "get_snippe_client", return_value=make_client(250000)):
			balance = settings.get_balance()

		self.assertEqual(balance, {"available_balance": 250000, "balance": 255000, "currency": "TZS"})
