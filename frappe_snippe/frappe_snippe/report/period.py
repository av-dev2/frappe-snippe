from datetime import date

import frappe
from frappe import _
from frappe.utils import get_timespan_date_range, getdate

PERIODS = (
	"Today",
	"Yesterday",
	"This Week",
	"Last Week",
	"This Month",
	"Last Month",
	"This Quarter",
	"Last Quarter",
	"This Year",
	"Last Year",
)


def get_period_dates(filters) -> tuple[date, date]:
	"""From and To dates of the report's Period filter, or of its Date Range when Period is Date Range."""
	if filters.period == "Date Range":
		if not filters.date_range:
			frappe.throw(_("Select a Date Range."))
		from_date, to_date = (getdate(value) for value in filters.date_range)
	elif filters.period in PERIODS:
		from_date, to_date = (getdate(value) for value in get_timespan_date_range(filters.period.lower()))
	else:
		frappe.throw(_("Select a Period."))

	if from_date > to_date:
		frappe.throw(_("From Date cannot be after To Date."))
	return from_date, to_date
