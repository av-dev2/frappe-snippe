import frappe
from frappe import _
from frappe.query_builder.functions import Count, Sum

from frappe_snippe.frappe_snippe.report.period import get_period_dates


def execute(filters=None):
	"""Sent payouts in the period, totalled by disbursement type, channel and status, with Snippe fees."""
	filters = frappe._dict(filters or {})
	filters.from_date, filters.to_date = get_period_dates(filters)
	frappe.has_permission("Company", doc=filters.company, throw=True)
	disbursement = frappe.qb.DocType("Payment Disbursement")
	item = frappe.qb.DocType("Payment Disbursement Item")
	rows = (
		frappe.qb.from_(item)
		.join(disbursement)
		.on(item.parent == disbursement.name)
		.select(
			disbursement.disbursement_type,
			item.channel,
			item.status,
			Count(item.name).as_("payouts"),
			Sum(item.amount).as_("amount"),
			Sum(item.fee_amount).as_("fee_amount"),
		)
		.where(
			(disbursement.company == filters.company)
			& (disbursement.docstatus == 1)
			& disbursement.posting_date.between(filters.from_date, filters.to_date)
		)
		.groupby(disbursement.disbursement_type, item.channel, item.status)
		.orderby(disbursement.disbursement_type)
		.orderby(item.channel)
		.orderby(item.status)
	).run(as_dict=True)

	return get_columns(), rows


def get_columns() -> list[dict]:
	return [
		{
			"fieldname": "disbursement_type",
			"label": _("Disbursement Type"),
			"fieldtype": "Data",
			"width": 160,
		},
		{"fieldname": "channel", "label": _("Channel"), "fieldtype": "Data", "width": 130},
		{"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 110},
		{"fieldname": "payouts", "label": _("Payouts"), "fieldtype": "Int", "width": 100},
		{"fieldname": "amount", "label": _("Amount"), "fieldtype": "Currency", "width": 150},
		{"fieldname": "fee_amount", "label": _("Snippe Fees"), "fieldtype": "Currency", "width": 130},
	]
