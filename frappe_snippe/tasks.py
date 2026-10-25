from collections import defaultdict
from functools import partial

import frappe
from frappe import _
from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification
from frappe.utils import add_days, add_to_date, flt, fmt_money, getdate, now_datetime, nowdate
from frappe.utils.user import get_users_with_role
from snippe import RateLimitError, SnippeError

from frappe_snippe.api.client import (
	BACKGROUND_RATE_LIMIT_WAITS,
	PAYMENT_STATUS,
	PAYOUT_STATUS,
	TooManyRecordsError,
	get_snippe_client,
	list_since,
)
from frappe_snippe.api.webhooks import process_inbound_webhook

RETRY_FILTERS = (
	{"docstatus": 1, "status": "Completed", "accounting_status": ("is", "not set")},
	{"docstatus": 1, "status": "Reversed", "accounting_status": "Booked"},
)
# A webhook job normally runs within seconds; older queued logs lost their job.
STALE_LOG_MINUTES = 10
LIST_LOOKBACK_DAYS = 7


def hourly():
	process_stale_payment_logs()
	reconcile_pending_collections()
	reconcile_pending_disbursements()
	resume_unsent_payouts()
	book_missing_payout_accounting()


def daily():
	alert_low_balances()


def alert_low_balances():
	"""Tell Accounts Managers when a company's Snippe balance is below its alert level, before payouts fail."""
	settings = frappe.get_all(
		"Snippe Setting",
		filters={"enable_payouts": 1, "low_balance_alert": (">", 0)},
		fields=["name", "company", "low_balance_alert"],
	)
	recipients = get_users_with_role("Accounts Manager")
	for setting in settings:
		try:
			with get_snippe_client(setting.company, BACKGROUND_RATE_LIMIT_WAITS) as client:
				balance = client.get_balance()
		except Exception:
			frappe.log_error(
				title="Snippe balance check failed",
				reference_doctype="Snippe Setting",
				reference_name=setting.name,
			)
			continue

		if flt(balance.available_balance) >= flt(setting.low_balance_alert):
			continue
		subject = _(
			"Snippe balance for {0} is low: {1} available, alert level {2}. Top up before payouts."
		).format(
			setting.company,
			fmt_money(balance.available_balance, currency=balance.currency),
			fmt_money(setting.low_balance_alert, currency=balance.currency),
		)
		enqueue_create_notification(
			# A manager restricted to other companies must not see this company's balance.
			[
				user
				for user in recipients
				if frappe.has_permission("Snippe Setting", doc=setting.name, user=user)
			],
			{
				"type": "Alert",
				"subject": subject,
				"document_type": "Snippe Setting",
				"document_name": setting.name,
			},
		)


def run_for_each(doctype, names, method, error_title):
	"""Call method with each document; a failure is rolled back and logged, and the rest still run.

	Snippe's rate limit stops the run, as every further call would fail too; the next run continues.
	"""
	for name in names:
		frappe.db.savepoint("snippe_hourly")
		try:
			method(frappe.get_doc(doctype, name))
		except RateLimitError:
			frappe.db.rollback(save_point="snippe_hourly")
			frappe.log_error(
				title="Snippe rate limit reached", reference_doctype=doctype, reference_name=name
			)
			return
		except Exception:
			frappe.db.rollback(save_point="snippe_hourly")
			frappe.log_error(title=error_title, reference_doctype=doctype, reference_name=name)


def process_stale_payment_logs():
	"""Process webhook logs whose queued job never ran, for example after a worker restart."""
	names = frappe.get_all(
		"Payment Log",
		filters={
			"processing_status": "Queued",
			"creation": ("<", add_to_date(now_datetime(), minutes=-STALE_LOG_MINUTES)),
		},
		pluck="name",
	)
	for name in names:
		frappe.get_doc("Payment Log", name).process()


def reconcile_pending_collections():
	"""Apply Snippe's status to collections whose webhook never arrived.

	List Payments is read first, as it carries the settlement fees that Get Payment Status lacks.
	"""
	collections = frappe.get_all(
		"Payment Collection",
		filters={"docstatus": 1, "status": "Pending", "snippe_reference": ("is", "set")},
		fields=["name", "company", "posting_date"],
	)
	by_company = defaultdict(list)
	for collection in collections:
		by_company[collection.company].append(collection)

	for company, company_collections in by_company.items():
		# Older stuck collections use the status check, so they cannot push the list past its page limit.
		oldest = max(
			min(collection.posting_date for collection in company_collections),
			getdate(add_days(nowdate(), -LIST_LOOKBACK_DAYS)),
		)
		payments = get_listed_payments(company, add_days(oldest, -1))
		run_for_each(
			"Payment Collection",
			[collection.name for collection in company_collections],
			partial(reconcile_collection, payments=payments),
			"Snippe collection status check failed",
		)


def get_listed_payments(company: str, from_date) -> dict:
	"""The company's Snippe payments since from_date; empty when the list cannot be read in full."""
	try:
		with get_snippe_client(company, BACKGROUND_RATE_LIMIT_WAITS) as client:
			return list_since(client.list_payment_data, from_date)
	except (SnippeError, TooManyRecordsError):
		frappe.log_error(
			title="Snippe payment list not read", reference_doctype="Company", reference_name=company
		)
		return {}


def reconcile_collection(collection, payments: dict):
	"""Book from the listed payment when there is one, else from Snippe's status check, which has no fee."""
	payment = payments.get(collection.snippe_reference)
	if payment:
		status, payload = PAYMENT_STATUS.get(payment["status"], "Pending"), payment
	else:
		with get_snippe_client(collection.company, BACKGROUND_RATE_LIMIT_WAITS) as client:
			status, payload = collection.get_snippe_update(client)

	if status != "Pending":
		process_inbound_webhook(collection.name, status, None, payload)


def reconcile_pending_disbursements():
	"""Apply Snippe's status to sent payouts whose webhook never arrived."""
	names = frappe.get_all(
		"Payment Disbursement",
		filters={"status": ("in", ["Processing", "Partially Completed"])},
		pluck="name",
	)
	run_for_each("Payment Disbursement", names, reconcile_disbursement, "Snippe payout status check failed")


def reconcile_disbursement(disbursement):
	updated = False
	with get_snippe_client(disbursement.company, BACKGROUND_RATE_LIMIT_WAITS) as client:
		for item in disbursement.items:
			if item.status != "Pending" or not item.snippe_reference:
				continue

			try:
				response = client.get_payout(item.snippe_reference)
			except RateLimitError:
				raise
			except SnippeError as e:
				frappe.log_error(
					title="Snippe payout reconcile failed",
					message=f"Payout {item.snippe_reference}: {e.message}",
					reference_doctype="Payment Disbursement",
					reference_name=disbursement.name,
				)
				continue

			new_status = PAYOUT_STATUS.get(response.status, "Pending")
			if new_status != item.status:
				disbursement.set_item_status(item, new_status, response.failure_reason)
				updated = True

	if updated:
		disbursement.update_status()

	disbursement.db_set("last_status_check", now_datetime())


def resume_unsent_payouts():
	"""Restart payout batches that stopped, for example on Snippe's rate limit or a network error."""
	names = frappe.get_all(
		"Payment Disbursement",
		filters=[
			["docstatus", "=", 1],
			["queued_on", "is", "set"],
			["Payment Disbursement Item", "status", "=", "Pending"],
			["Payment Disbursement Item", "snippe_reference", "is", "not set"],
		],
		pluck="name",
		distinct=True,
	)
	run_for_each(
		"Payment Disbursement",
		names,
		lambda disbursement: disbursement.resume_unsent_payouts(),
		"Snippe payout resume failed",
	)


def book_missing_payout_accounting():
	"""Retry payout bookings and reversals that failed earlier."""
	row_names_by_parent = defaultdict(set)
	for filters in RETRY_FILTERS:
		for row in frappe.get_all("Payment Disbursement Item", filters=filters, fields=["parent", "name"]):
			row_names_by_parent[row.parent].add(row.name)

	for parent, row_names in row_names_by_parent.items():
		disbursement = frappe.get_doc("Payment Disbursement", parent, for_update=True)
		for row in disbursement.items:
			if row.name in row_names:
				disbursement.book_item(row)
