frappe.query_reports["Payment Reconciliation"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Snippe Setting",
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
			default: "Today",
			reqd: 1,
		},
		{
			fieldname: "date_range",
			label: __("Date Range"),
			fieldtype: "DateRange",
			depends_on: "eval:doc.period == 'Date Range'",
		},
		{ fieldname: "show_matched", label: __("Show Matched"), fieldtype: "Check" },
	],

	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "result" && data) {
			const color = data.result === "Matched" ? "green" : "red";
			value = `<span class="indicator-pill ${color}">${value}</span>`;
		}
		return value;
	},
};
