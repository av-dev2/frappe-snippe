function add_snippe_disbursement_button(frm, amount, default_channel) {
	if (frm.doc.docstatus === 1) {
		// Need to check if a pending disbursement already exists
		frappe.db.get_value(
			"Payment Disbursement Item",
			{
				reference_doctype: frm.doc.doctype,
				reference_name: frm.doc.name,
				status: "Pending",
			},
			"name",
			function (r) {
				if (!r || !r.name) {
					frm.add_custom_button(
						__("Disburse via Snippe"),
						function () {
							// get recipient name depending on doctype
							let recipient =
								frm.doc.employee_name ||
								frm.doc.supplier_name ||
								frm.doc.applicant_name ||
								"";
							frappe.new_doc("Payment Disbursement", {
								company: frm.doc.company,
								disbursement_type: frm.doc.doctype,
								items: [
									{
										reference_doctype: frm.doc.doctype,
										reference_name: frm.doc.name,
										recipient_name: recipient,
										amount: amount,
										channel: default_channel || "Mobile Money",
									},
								],
							});
						},
						__("Create")
					);
				}
			}
		);
	}
}

frappe.ui.form.on("Purchase Invoice", {
	refresh: function (frm) {
		if (frm.doc.outstanding_amount > 0) {
			add_snippe_disbursement_button(frm, frm.doc.outstanding_amount, "Bank Transfer");
		}
	},
});

frappe.ui.form.on("Salary Slip", {
	refresh: function (frm) {
		add_snippe_disbursement_button(
			frm,
			frm.doc.rounded_total || frm.doc.net_pay,
			"Bank Transfer"
		);
	},
});

frappe.ui.form.on("Employee Advance", {
	refresh: function (frm) {
		const left_to_pay = frm.doc.advance_amount - frm.doc.paid_amount;
		if (left_to_pay > 0) {
			add_snippe_disbursement_button(frm, left_to_pay, "Mobile Money");
		}
	},
});

frappe.ui.form.on("Expense Claim", {
	refresh: function (frm) {
		const left_to_pay = frm.doc.grand_total - frm.doc.total_amount_reimbursed;
		if (left_to_pay > 0) {
			add_snippe_disbursement_button(frm, left_to_pay, "Mobile Money");
		}
	},
});

// Payroll Entry needs a special function to pull all unpaid Salary Slips
frappe.ui.form.on("Payroll Entry", {
	refresh: function (frm) {
		if (frm.doc.docstatus === 1) {
			frm.add_custom_button(
				__("Disburse via Snippe"),
				function () {
					frappe.call({
						method: "frappe.client.get_list",
						args: {
							doctype: "Salary Slip",
							filters: {
								payroll_entry: frm.doc.name,
								docstatus: 1,
							},
							fields: ["name", "employee_name", "rounded_total", "net_pay"],
						},
						callback: function (r) {
							if (r.message && r.message.length > 0) {
								let items = r.message.map((slip) => {
									return {
										reference_doctype: "Salary Slip",
										reference_name: slip.name,
										recipient_name: slip.employee_name,
										amount: slip.rounded_total || slip.net_pay,
										channel: "Bank Transfer",
									};
								});

								frappe.new_doc("Payment Disbursement", {
									company: frm.doc.company,
									disbursement_type: "Salary",
									payroll_entry: frm.doc.name,
									items: items,
								});
							} else {
								frappe.msgprint(
									__("No unpaid salary slips found for this payroll.")
								);
							}
						},
					});
				},
				__("Create")
			);
		}
	},
});
