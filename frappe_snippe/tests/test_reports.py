import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.desk.desktop import get_desktop_page
from snippe import Balance

from frappe_snippe.api.client import TooManyRecordsError, list_since
from frappe_snippe.frappe_snippe.doctype.snippe_setting import snippe_setting
from frappe_snippe.frappe_snippe.report.payment_collection_summary import payment_collection_summary
from frappe_snippe.frappe_snippe.report.payment_reconciliation import payment_reconciliation
from frappe_snippe.frappe_snippe.report.period import get_period_dates
from frappe_snippe.tests.test_collection_customer import make_customer
from frappe_snippe.tests.test_paid_amount import make_paid_collection
from frappe_snippe.tests.test_snippe_setting import (
	TZS_COMPANY,
	TzsCompanyTestCase,
	make_phone_mode_of_payment,
	make_snippe_setting,
)

TODAY = frappe.utils.nowdate()
FILTERS = {"company": TZS_COMPANY, "period": "Today"}


def snippe_item(reference, status, amount, created_at=f"{TODAY}T09:00:00Z"):
	return {
		"reference": reference,
		"status": status,
		"amount": {"value": amount, "currency": "TZS"},
		"created_at": created_at,
	}


def make_client(payments=(), payouts=(), available_balance=0):
	client = MagicMock()
	client.__enter__.return_value = client
	client.list_payment_data.return_value = {"items": list(payments), "total": len(payments)}
	client.list_payout_data.return_value = {"items": list(payouts), "total": len(payouts)}
	client.get_balance.return_value = Balance(
		available_balance=available_balance, balance=available_balance, currency="TZS"
	)
	return client


class ReportTestCase(TzsCompanyTestCase):
	"""Each test sees only its own collections; reports read every collection of the company."""

	def setUp(self):
		frappe.db.savepoint("report_test")
		make_snippe_setting()
		make_phone_mode_of_payment()
		self.customer = make_customer().name

	def tearDown(self):
		frappe.db.rollback(save_point="report_test")

	def make_submitted_collection(self, reference, status, amount):
		collection = make_paid_collection(self.customer, amount)
		collection.db_set({"docstatus": 1, "status": status, "snippe_reference": reference})
		return collection


class TestSnippeReconciliation(ReportTestCase):
	def run_report(self, client, **filters):
		with (
			patch.object(payment_reconciliation, "get_snippe_client", return_value=client),
			patch.object(snippe_setting, "get_snippe_client", return_value=client),
			patch.object(snippe_setting, "get_balance_on", return_value=0),
		):
			_columns, rows, _message, _chart, summary = payment_reconciliation.execute(
				{**FILTERS, "show_matched": 1, **filters}
			)
		return {row["snippe_reference"]: row["result"] for row in rows}, summary

	def test_each_payment_gets_its_result(self):
		self.make_submitted_collection("pi_match", "Completed", 5000)
		self.make_submitted_collection("pi_late", "Pending", 7000)
		self.make_submitted_collection("pi_short", "Completed", 9000)
		self.make_submitted_collection("pi_local", "Pending", 1000)
		frappe.get_doc(
			{
				"doctype": "Payment Log",
				"company": TZS_COMPANY,
				"event_id": "evt_review_report",
				"event_type": "payment.completed",
				"direction": "Inbound",
				"snippe_reference": "pi_review",
				"status": "completed",
				"amount": 3000,
				"processing_status": "Needs Review",
			}
		).insert(ignore_permissions=True)
		client = make_client(
			payments=[
				snippe_item("pi_match", "completed", 5000),
				snippe_item("pi_late", "completed", 7000),
				snippe_item("pi_short", "completed", 8000),
				snippe_item("pi_review", "completed", 3000),
				snippe_item("pi_unknown", "completed", 2000),
			]
		)

		results, _summary = self.run_report(client)

		self.assertEqual(
			results,
			{
				"pi_match": "Matched",
				"pi_late": "Status differs",
				"pi_short": "Amount differs",
				"pi_local": "Missing in Snippe",
				"pi_review": "Needs Review",
				"pi_unknown": "Missing in ERPNext",
			},
		)

	def test_matched_rows_are_hidden_unless_asked_for(self):
		self.make_submitted_collection("pi_match", "Completed", 5000)
		client = make_client(payments=[snippe_item("pi_match", "completed", 5000)])
		results, _summary = self.run_report(client, show_matched=0)
		self.assertEqual(results, {})

	def test_summary_compares_the_snippe_balance_with_the_wallet_account(self):
		_results, summary = self.run_report(make_client(available_balance=150000))
		values = {item["label"]: item["value"] for item in summary}
		self.assertEqual(
			(
				values["Snippe Available Balance"],
				values["Wallet Account Balance"],
				values["Balance Difference"],
			),
			(150000, 0, 150000),
		)

	def test_payout_result_needs_its_accounting(self):
		completed = frappe._dict(status="Completed", accounting_status=None)
		reversed_booked = frappe._dict(status="Reversed", accounting_status="Booked")
		failed = frappe._dict(status="Failed", accounting_status=None)
		self.assertEqual(
			[payment_reconciliation.is_payout_unbooked(row) for row in (completed, reversed_booked, failed)],
			[True, True, False],
		)

	def test_listing_stops_at_the_first_page_older_than_the_from_date(self):
		older = snippe_item("pi_old", "completed", 100, created_at="2020-01-01T09:00:00Z")
		list_page = MagicMock(
			return_value={"items": [snippe_item("pi_new", "completed", 100), older], "total": 500}
		)

		records = list_since(list_page, frappe.utils.getdate(TODAY))

		self.assertEqual(set(records), {"pi_new", "pi_old"})
		list_page.assert_called_once_with(limit=100, offset=0)

	def test_listing_has_a_page_limit(self):
		page = {"items": [snippe_item("pi_same", "completed", 100)], "total": 100000}
		with self.assertRaises(TooManyRecordsError):
			list_since(MagicMock(return_value=page), frappe.utils.getdate(TODAY))

	def test_too_many_records_ask_for_a_shorter_period(self):
		page = {"items": [snippe_item("pi_same", "completed", 100)], "total": 100000}
		client = make_client()
		client.list_payment_data.return_value = page
		with self.assertRaisesRegex(frappe.ValidationError, "Choose a shorter Period"):
			self.run_report(client)


class TestPeriod(TzsCompanyTestCase):
	def get_dates(self, **filters):
		return get_period_dates(frappe._dict(filters))

	def test_named_period_uses_frappe_timespans(self):
		today = frappe.utils.getdate()
		self.assertEqual(
			self.get_dates(period="This Month"),
			(frappe.utils.get_first_day(today), frappe.utils.get_last_day(today)),
		)

	def test_date_range_is_used_only_for_the_date_range_period(self):
		self.assertEqual(
			self.get_dates(period="Date Range", date_range=["2026-01-01", "2026-01-31"]),
			(frappe.utils.getdate("2026-01-01"), frappe.utils.getdate("2026-01-31")),
		)
		with self.assertRaisesRegex(frappe.ValidationError, "Select a Date Range"):
			self.get_dates(period="Date Range")

	def test_unknown_period_is_rejected(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Select a Period"):
			self.get_dates(period="next 7 days")


class TestCollectionSummary(ReportTestCase):
	def test_collections_are_totalled_by_method_and_status(self):
		self.make_submitted_collection("pi_sum_1", "Completed", 5000)
		self.make_submitted_collection("pi_sum_2", "Completed", 3000)
		self.make_submitted_collection("pi_sum_3", "Failed", 1000)

		_columns, rows = payment_collection_summary.execute(FILTERS)

		self.assertEqual(
			[(row.payment_method, row.status, row.collections, row.amount) for row in rows],
			[("Mobile Money", "Completed", 2, 8000), ("Mobile Money", "Failed", 1, 1000)],
		)


class TestWorkspaceVisibility(TzsCompanyTestCase):
	def get_page_for(self, role):
		email = f"snippe-{frappe.scrub(role)}@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{"doctype": "User", "email": email, "first_name": role, "send_welcome_email": 0}
			).insert(ignore_permissions=True)
		user = frappe.get_doc("User", email)
		user.roles = []
		user.append("roles", {"role": role})
		user.save(ignore_permissions=True)

		frappe.set_user(email)
		try:
			page = get_desktop_page(json.dumps({"name": "Snippe", "title": "Snippe", "public": 1}))
		finally:
			frappe.set_user("Administrator")
		cards = {card["label"]: [link["label"] for link in card["links"]] for card in page["cards"]["items"]}
		return [card.number_card_name for card in page["number_cards"]["items"]], cards

	def test_accounts_user_sees_documents_but_not_logs_or_reports(self):
		number_cards, cards = self.get_page_for("Accounts User")

		self.assertIn("Collected This Month", number_cards)
		self.assertNotIn("Payments Needing Review", number_cards)
		self.assertNotIn("Reports", cards)
		self.assertNotIn("Payment Log", cards["Accounting"])

	def test_accounts_manager_sees_reports_and_log_cards(self):
		number_cards, cards = self.get_page_for("Accounts Manager")

		self.assertIn("Payments Needing Review", number_cards)
		self.assertEqual(
			cards["Reports"],
			["Payment Reconciliation", "Payment Collection Summary", "Payment Payout Summary"],
		)

	def test_administrator_sees_every_card(self):
		# Frappe hides every card from Administrator when one linked DocType is missing.
		page = get_desktop_page(json.dumps({"name": "Snippe", "title": "Snippe", "public": 1}))
		self.assertEqual(
			[card["label"] for card in page["cards"]["items"]],
			["Collections", "Payouts", "Accounting", "Setup", "Reports"],
		)
