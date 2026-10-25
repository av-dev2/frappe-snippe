import frappe
from frappe import _
from frappe.query_builder.functions import Count, Sum

from frappe_snippe.frappe_snippe.report.period import get_period_dates


def execute(filters=None):
	"""Submitted collections in the period, totalled by payment method and status."""
	filters = frappe._dict(filters or {})
	filters.from_date, filters.to_date = get_period_dates(filters)
	frappe.has_permission("Company", doc=filters.company, throw=True)
	collection = frappe.qb.DocType("Payment Collection")
	rows = (
		frappe.qb.from_(collection)
		.select(
			collection.payment_method,
			collection.status,
			Count(collection.name).as_("collections"),
			Sum(collection.total_amount).as_("amount"),
		)
		.where(
			(collection.company == filters.company)
			& (collection.docstatus == 1)
			& collection.posting_date.between(filters.from_date, filters.to_date)
		)
		.groupby(collection.payment_method, collection.status)
		.orderby(collection.payment_method)
		.orderby(collection.status)
	).run(as_dict=True)

	return get_columns(), rows


def get_columns() -> list[dict]:
	return [
		{"fieldname": "payment_method", "label": _("Payment Method"), "fieldtype": "Data", "width": 160},
		{"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 120},
		{"fieldname": "collections", "label": _("Collections"), "fieldtype": "Int", "width": 120},
		{"fieldname": "amount", "label": _("Amount"), "fieldtype": "Currency", "width": 150},
	]
