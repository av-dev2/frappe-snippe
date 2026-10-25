from unittest.mock import patch

import frappe

from frappe_snippe import tasks
from frappe_snippe.accounting.wallet_entries import post_wallet_entry
from frappe_snippe.tests.test_fees_and_balance import make_client
from frappe_snippe.tests.test_invoice_payment import make_sales_team_account
from frappe_snippe.tests.test_payout_flows import get_gl_lines
from frappe_snippe.tests.test_snippe_setting import (
	TZS_CASH,
	TZS_COMPANY,
	TZS_FEES,
	TzsCompanyTestCase,
	make_snippe_setting,
)


class TestWalletEntry(TzsCompanyTestCase):
	def test_amounts_that_cancel_out_post_nothing(self):
		entry = post_wallet_entry(
			company=TZS_COMPANY,
			posting_date=frappe.utils.nowdate(),
			reference="pi_none",
			remark="Nothing moved",
			amounts={TZS_CASH: 0, TZS_FEES: 0},
		)
		self.assertIsNone(entry)


class TestRecordWalletTransfer(TzsCompanyTestCase):
	def setUp(self):
		self.setting = make_snippe_setting()
		self.bank = make_sales_team_account()

	def record(self, transfer_type, amount, fee=0, bank=None, reference=None):
		return self.setting.record_wallet_transfer(
			transfer_type,
			bank or self.bank,
			amount,
			frappe.utils.nowdate(),
			reference or frappe.generate_hash(),
			fee,
		)

	def test_top_up_moves_money_from_the_bank_into_the_wallet(self):
		journal_entry = frappe.get_doc("Journal Entry", self.record("Top-up", 500000, reference="TRF-1"))

		self.assertEqual((journal_entry.voucher_type, journal_entry.cheque_no), ("Contra Entry", "TRF-1"))
		self.assertEqual(get_gl_lines(journal_entry.name), {(TZS_CASH, 500000, 0), (self.bank, 0, 500000)})

	def test_withdrawal_fee_comes_out_of_the_wallet(self):
		journal_entry = self.record("Withdrawal", 200000, fee=1500)

		self.assertEqual(
			get_gl_lines(journal_entry),
			{(self.bank, 200000, 0), (TZS_FEES, 1500, 0), (TZS_CASH, 0, 201500)},
		)

	def test_same_bank_reference_is_recorded_once(self):
		self.record("Top-up", 1000, reference="TRF-TWICE")
		with self.assertRaisesRegex(frappe.ValidationError, "Each reference is recorded once"):
			self.record("Top-up", 1000, reference="TRF-TWICE")

	def test_wallet_account_cannot_be_the_bank_account(self):
		with self.assertRaisesRegex(frappe.ValidationError, "not the Wallet Account"):
			self.record("Top-up", 1000, bank=TZS_CASH)

	def test_account_must_be_a_bank_or_cash_ledger(self):
		with self.assertRaisesRegex(frappe.ValidationError, "must be a Bank or Cash ledger"):
			self.record("Top-up", 1000, bank=TZS_FEES)

	def test_amount_must_be_positive(self):
		with self.assertRaisesRegex(frappe.ValidationError, "more than 0"):
			self.record("Withdrawal", 0)

	def test_unknown_transfer_type_is_rejected(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Top-up or Withdrawal"):
			self.record("Refund", 1000)


class TestLowBalanceAlert(TzsCompanyTestCase):
	def setUp(self):
		make_snippe_setting(low_balance_alert=1000000)
		frappe.db.delete("Notification Log", {"document_type": "Snippe Setting"})

	def alert(self, available_balance):
		with (
			patch.object(tasks, "get_snippe_client", return_value=make_client(available_balance)),
			patch.object(tasks, "get_users_with_role", return_value=["test@erpnext.com"]),
		):
			tasks.alert_low_balances()
		return frappe.get_all(
			"Notification Log",
			filters={"document_type": "Snippe Setting", "document_name": TZS_COMPANY},
			fields=["for_user", "subject"],
		)

	def test_balance_below_the_alert_level_notifies_accounts_managers(self):
		[notification] = self.alert(400000)
		self.assertEqual(notification.for_user, "test@erpnext.com")
		self.assertIn("is low", notification.subject)

	def test_balance_at_the_alert_level_sends_nothing(self):
		self.assertEqual(self.alert(1000000), [])

	def test_setting_without_an_alert_level_is_not_checked(self):
		make_snippe_setting(low_balance_alert=0)
		with patch.object(tasks, "get_snippe_client") as get_client:
			tasks.alert_low_balances()
		get_client.assert_not_called()
