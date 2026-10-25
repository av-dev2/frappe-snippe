from datetime import date, datetime
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_system_timezone, getdate

from frappe_snippe.api.client import (
	PAYMENT_STATUS,
	PAYOUT_STATUS,
	get_snippe_client,
	snippe_errors_as_messages,
)
from frappe_snippe.frappe_snippe.report.period import get_period_dates

PAGE_SIZE = 100
# Two lists of 20 pages stay inside Snippe's 60 requests a minute.
MAX_PAGES = 20
REVIEW_STATUSES = ("Needs Review", "Failed")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	from_date, to_date = get_period_dates(filters)
	filters.update(from_date=from_date, to_date=to_date)
	frappe.get_doc("Snippe Setting", filters.company).check_permission("read")

	with get_snippe_client(filters.company) as client, snippe_errors_as_messages():
		# A day earlier, so a payment late on the day before still matches a document posted on From Date.
		payments = get_snippe_records(client.list_payment_data, add_days(from_date, -1))
		payouts = get_snippe_records(client.list_payout_data, add_days(from_date, -1))

	rows = [
		*compare(
			"Payment", payments, get_erpnext_payments(filters, payments), PAYMENT_STATUS, from_date, to_date
		),
		*compare("Payout", payouts, get_erpnext_payouts(filters, payouts), PAYOUT_STATUS, from_date, to_date),
	]
	rows.sort(key=lambda row: row["date"], reverse=True)
	summary = get_summary(filters.company, rows)
	if not filters.show_matched:
		rows = [row for row in rows if row["result"] != "Matched"]

	return get_columns(), rows, None, None, summary


def get_snippe_records(list_page, from_date: date) -> dict:
	"""Snippe's records from from_date onwards by reference; lists come newest first."""
	records = {}
	for page in range(MAX_PAGES):
		data = list_page(limit=PAGE_SIZE, offset=page * PAGE_SIZE)
		items = data.get("items") or []
		for item in items:
			item["date"] = to_local_date(item["created_at"])
			records[item["reference"]] = item

		if not items or items[-1]["date"] < from_date or (page + 1) * PAGE_SIZE >= cint(data.get("total")):
			return records

	frappe.throw(
		_("Snippe has more than {0} records since {1}. Choose a shorter Period.").format(
			MAX_PAGES * PAGE_SIZE, frappe.format(from_date, "Date")
		)
	)


def to_local_date(timestamp: str) -> date:
	moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
	return moment.astimezone(ZoneInfo(get_system_timezone())).date()


def get_erpnext_payments(filters, snippe_payments: dict) -> dict:
	"""ERPNext's side of each payment: its Payment Collection, else the Payment Log of its webhook."""
	records = {}
	references = list(snippe_payments) or [""]
	logs = frappe.get_all(
		"Payment Log",
		filters={
			"company": filters.company,
			"direction": "Inbound",
			"snippe_reference": ("in", references),
		},
		fields=["name", "snippe_reference", "status", "amount", "processing_status", "creation"],
		order_by="creation",
	)
	for log in logs:
		records[log.snippe_reference] = {
			"document_type": "Payment Log",
			"document": log.name,
			"status": PAYMENT_STATUS.get(log.status),
			"amount": log.amount,
			"date": getdate(log.creation),
			"issue": log.processing_status if log.processing_status in REVIEW_STATUSES else None,
		}

	# A Hosted Session's reference is the session's, not the payment's, so its log above stands for it.
	collections = frappe.get_all(
		"Payment Collection",
		filters={
			"company": filters.company,
			"docstatus": 1,
			"payment_method": ("!=", "Hosted Session"),
			"snippe_reference": ("is", "set"),
		},
		or_filters={
			"snippe_reference": ("in", references),
			"posting_date": ("between", [filters.from_date, filters.to_date]),
		},
		fields=["name", "snippe_reference", "status", "total_amount", "posting_date"],
	)
	for collection in collections:
		log = records.get(collection.snippe_reference) or {}
		records[collection.snippe_reference] = {
			"document_type": "Payment Collection",
			"document": collection.name,
			"status": collection.status,
			"amount": collection.total_amount,
			"date": collection.posting_date,
			"issue": log.get("issue"),
		}
	return records


def get_erpnext_payouts(filters, snippe_payouts: dict) -> dict:
	disbursement = frappe.qb.DocType("Payment Disbursement")
	item = frappe.qb.DocType("Payment Disbursement Item")
	references = list(snippe_payouts) or [""]
	rows = (
		frappe.qb.from_(item)
		.join(disbursement)
		.on(item.parent == disbursement.name)
		.select(
			disbursement.name,
			disbursement.posting_date,
			item.snippe_reference,
			item.status,
			item.amount,
			item.accounting_status,
		)
		.where(
			(disbursement.company == filters.company)
			& (disbursement.docstatus == 1)
			& (item.snippe_reference != "")
			& (
				item.snippe_reference.isin(references)
				| disbursement.posting_date.between(filters.from_date, filters.to_date)
			)
		)
	).run(as_dict=True)

	return {
		row.snippe_reference: {
			"document_type": "Payment Disbursement",
			"document": row.name,
			"status": row.status,
			"amount": row.amount,
			"date": row.posting_date,
			"issue": "Not booked" if is_payout_unbooked(row) else None,
		}
		for row in rows
	}


def is_payout_unbooked(row) -> bool:
	if row.status == "Completed":
		return row.accounting_status != "Booked"
	return row.status == "Reversed" and row.accounting_status == "Booked"


def compare(
	record_type: str, snippe: dict, erpnext: dict, status_map: dict, from_date: date, to_date: date
) -> list[dict]:
	rows = []
	for reference in snippe.keys() | erpnext.keys():
		remote, local = snippe.get(reference), erpnext.get(reference)
		row_date = remote["date"] if remote else getdate(local["date"])
		if not from_date <= row_date <= to_date:
			continue

		local = local or {}
		rows.append(
			{
				"date": row_date,
				"record_type": record_type,
				"snippe_reference": reference,
				"snippe_status": status_map.get(remote["status"], remote["status"]) if remote else None,
				"snippe_amount": flt(remote["amount"]["value"]) if remote else None,
				"document_type": local.get("document_type"),
				"document": local.get("document"),
				"erpnext_status": local.get("status"),
				"erpnext_amount": local.get("amount"),
				"result": get_result(remote, local, status_map),
			}
		)
	return rows


def get_result(remote: dict | None, local: dict, status_map: dict) -> str:
	if not local:
		return "Missing in ERPNext"
	if not remote:
		return "Missing in Snippe"
	if status_map.get(remote["status"]) != local["status"]:
		return "Status differs"
	# A custom-amount collection has no amount until it is paid.
	if flt(local["amount"]) and flt(remote["amount"]["value"]) != flt(local["amount"]):
		return "Amount differs"
	return local["issue"] or "Matched"


def get_summary(company: str, rows: list[dict]) -> list[dict]:
	balance = frappe.get_doc("Snippe Setting", company).get_balance()
	mismatches = sum(row["result"] != "Matched" for row in rows)
	currency = balance["currency"]
	return [
		{
			"label": _("Snippe Available Balance"),
			"value": balance["available_balance"],
			"datatype": "Currency",
			"currency": currency,
		},
		{
			"label": _("Wallet Account Balance"),
			"value": balance["wallet_balance"],
			"datatype": "Currency",
			"currency": currency,
		},
		{
			"label": _("Balance Difference"),
			"value": balance["difference"],
			"datatype": "Currency",
			"currency": currency,
			"indicator": "Red" if balance["difference"] else "Green",
		},
		{
			"label": _("Mismatches"),
			"value": mismatches,
			"datatype": "Int",
			"indicator": "Red" if mismatches else "Green",
		},
	]


def get_columns() -> list[dict]:
	return [
		{"fieldname": "date", "label": _("Date"), "fieldtype": "Date", "width": 100},
		{"fieldname": "record_type", "label": _("Type"), "fieldtype": "Data", "width": 90},
		{"fieldname": "snippe_reference", "label": _("Snippe Reference"), "fieldtype": "Data", "width": 180},
		{"fieldname": "snippe_status", "label": _("Snippe Status"), "fieldtype": "Data", "width": 110},
		{"fieldname": "snippe_amount", "label": _("Snippe Amount"), "fieldtype": "Currency", "width": 120},
		{
			"fieldname": "document_type",
			"label": _("Document Type"),
			"fieldtype": "Link",
			"options": "DocType",
			"width": 150,
		},
		{
			"fieldname": "document",
			"label": _("Document"),
			"fieldtype": "Dynamic Link",
			"options": "document_type",
			"width": 180,
		},
		{"fieldname": "erpnext_status", "label": _("ERPNext Status"), "fieldtype": "Data", "width": 110},
		{"fieldname": "erpnext_amount", "label": _("ERPNext Amount"), "fieldtype": "Currency", "width": 120},
		{"fieldname": "result", "label": _("Result"), "fieldtype": "Data", "width": 150},
	]
