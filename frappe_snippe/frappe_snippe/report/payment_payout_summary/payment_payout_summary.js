frappe.query_reports["Payment Payout Summary"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1,
		},
		{
			fieldname: "period",
			label: __("Period"),
			fieldtype: "Select",
			options: [
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
				"Date Range",
			].map((value) => ({ value, label: __(value) })),
			default: "This Month",
			reqd: 1,
		},
		{
			fieldname: "date_range",
			label: __("Date Range"),
			fieldtype: "DateRange",
			depends_on: "eval:doc.period == 'Date Range'",
		},
	],
};
