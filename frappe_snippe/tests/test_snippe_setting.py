from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_snippe.api.client import extend_bootinfo, get_snippe_client, get_snippe_setting

TZS_COMPANY = "Snippe Test Tanzania"
TZS_ABBR = "STZ"
TZS_CASH = f"Cash - {TZS_ABBR}"
TZS_FEES = f"Administrative Expenses - {TZS_ABBR}"
USD_COMPANY = "Wind Power LLC"
USD_CASH = "Cash - WP"
USD_FEES = "Administrative Expenses - WP"


def get_tzs_company():
	"""A TZS company that stays on the test site, like ERPNext's own Wind Power LLC."""
	if not frappe.db.exists("Company", TZS_COMPANY):
		frappe.db.set_value("Currency", "TZS", "enabled", 1)
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": TZS_COMPANY,
				"abbr": TZS_ABBR,
				"default_currency": "TZS",
				"country": "Tanzania",
				"create_chart_of_accounts_based_on": "Standard Template",
				"chart_of_accounts": "Standard",
			}
		).insert()
		frappe.db.commit()  # nosemgrep

	return TZS_COMPANY


def make_snippe_setting(company=TZS_COMPANY, ignore_validate=False, **values):
	"""Replace the company's Snippe Setting with a fully enabled one, changed by values."""
	frappe.delete_doc_if_exists("Snippe Setting", company)
	accounts = {"wallet_account": TZS_CASH, "fees_account": TZS_FEES} if company == TZS_COMPANY else {}
	setting = frappe.get_doc(
		{
			"doctype": "Snippe Setting",
			"company": company,
			"enable_collection": 1,
			"enable_payouts": 1,
			"api_key": "snp_test",
			"webhook_secret": "whsec_test",
			**accounts,
			**values,
		}
	)
	setting.flags.ignore_validate = ignore_validate
	return setting.insert()


def make_payout_setting(**values):
	"""Payouts-only Snippe Setting for ERPNext's USD test company, whose ledgers payout tests post to.

	That company has no TZS account, so the setting skips the TZS check.
	"""
	return make_snippe_setting(
		USD_COMPANY,
		ignore_validate=True,
		enable_collection=0,
		wallet_account=USD_CASH,
		fees_account=USD_FEES,
		**values,
	)


class TzsCompanyTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		get_tzs_company()
		super().setUpClass()


class TestSnippeSettingValidation(TzsCompanyTestCase):
	def assert_rejected(self, message, **values):
		with self.assertRaisesRegex(frappe.ValidationError, message):
			make_snippe_setting(**values)

	def test_company_is_the_setting_name(self):
		self.assertEqual(make_snippe_setting().name, TZS_COMPANY)

	def test_account_of_another_company_is_rejected(self):
		self.assert_rejected("ledger account of Company", wallet_account="Cash - WP")

	def test_non_tzs_gl_account_is_rejected(self):
		with self.assertRaisesRegex(frappe.ValidationError, "is in USD"):
			make_snippe_setting(
				USD_COMPANY,
				wallet_account="Cash - WP",
				fees_account="Administrative Expenses - WP",
			)

	def test_wallet_account_must_be_bank_or_cash(self):
		self.assert_rejected("Wallet Account must be a Bank or Cash", wallet_account=TZS_FEES)

	def test_fees_account_must_be_a_ledger_expense_account(self):
		self.assert_rejected("Expense account", fees_account=TZS_CASH)
		self.assert_rejected("ledger account", fees_account=f"Indirect Expenses - {TZS_ABBR}")

	def test_enabled_feature_needs_its_credentials_and_accounts(self):
		self.assert_rejected("API Key", api_key=None)
		for feature in ("enable_collection", "enable_payouts"):
			self.assert_rejected("Wallet Account", wallet_account=None, **{feature: 0})

	def test_disabled_setting_needs_nothing(self):
		make_snippe_setting(
			enable_collection=0, enable_payouts=0, api_key=None, webhook_secret=None, fees_account=None
		)


class TestSnippeSettingUse(TzsCompanyTestCase):
	def setUp(self):
		frappe.delete_doc_if_exists("Snippe Setting", USD_COMPANY)

	def test_company_without_setting_stops_snippe_work(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Create a Snippe Setting for Company Wind Power"):
			get_snippe_setting(USD_COMPANY)

	def test_client_uses_the_company_key_and_sdk_base_url(self):
		make_snippe_setting(api_key="snp_tz")
		with get_snippe_client(TZS_COMPANY) as client:
			self.assertEqual((client.api_key, client.base_url), ("snp_tz", "https://api.snippe.sh/api/v1"))

	def test_disabled_feature_stops_new_work(self):
		setting = make_snippe_setting(enable_payouts=0)
		setting.validate_collection_enabled()
		with self.assertRaisesRegex(frappe.ValidationError, "Payouts through Snippe are disabled"):
			setting.validate_payouts_enabled()

	def test_collection_for_a_company_without_collection_is_blocked(self):
		make_snippe_setting(enable_collection=0)
		collection = frappe.get_doc({"doctype": "Payment Collection", "company": TZS_COMPANY})
		with self.assertRaisesRegex(frappe.ValidationError, "Payment collection through Snippe is disabled"):
			collection.validate()

	def test_payouts_are_credited_from_the_wallet_account(self):
		make_snippe_setting()
		disbursement = frappe.get_doc(
			{"doctype": "Payment Disbursement", "company": TZS_COMPANY, "paid_from_account": TZS_FEES}
		)
		disbursement.validate()
		self.assertEqual(disbursement.paid_from_account, TZS_CASH)

	def test_disbursement_for_a_company_without_payouts_is_blocked(self):
		make_snippe_setting(enable_payouts=0)
		disbursement = frappe.get_doc({"doctype": "Payment Disbursement", "company": TZS_COMPANY})
		with self.assertRaisesRegex(frappe.ValidationError, "Payouts through Snippe are disabled"):
			disbursement.validate()

	def test_boot_tells_forms_which_features_each_company_uses(self):
		make_snippe_setting(enable_payouts=0)
		bootinfo = frappe._dict()
		extend_bootinfo(bootinfo)

		features = bootinfo.snippe_features[TZS_COMPANY]
		self.assertEqual((features.enable_collection, features.enable_payouts), (1, 0))
		self.assertNotIn(USD_COMPANY, bootinfo.snippe_features)

	def test_missing_account_names_the_field(self):
		setting = make_snippe_setting()
		setting.db_set("wallet_account", None)
		with self.assertRaisesRegex(frappe.ValidationError, "Set the Wallet Account"):
			setting.get_account("wallet_account")
