function add_snippe_disbursement_button(frm, amount_field, default_channel) {
	if (frm.doc.docstatus === 1 && !frm.doc.disbursed) {
		// Need to check if a pending disbursement already exists
		frappe.db.get_value(
			"Payment Disbursement Item",
			{
				reference_doctype: frm.doc.doctype,
				reference_name: frm.doc.name,
				status: ["in", ["Pending", "Processing"]],
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
							let amount = frm.doc[amount_field] || 0;

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
			add_snippe_disbursement_button(frm, "outstanding_amount", "Bank Transfer");
		}
	},
});

frappe.ui.form.on("Salary Slip", {
	refresh: function (frm) {
		if (frm.doc.status !== "Paid") {
			add_snippe_disbursement_button(frm, "rounded_total", "Bank Transfer");
		}
	},
});

frappe.ui.form.on("Employee Advance", {
	refresh: function (frm) {
		if (frm.doc.advance_amount > frm.doc.paid_amount) {
			add_snippe_disbursement_button(frm, "advance_amount", "Mobile Money"); // Or remaining amount
		}
	},
});

frappe.ui.form.on("Expense Claim", {
	refresh: function (frm) {
		if (frm.doc.total_sanctioned_amount > frm.doc.total_amount_reimbursed) {
			add_snippe_disbursement_button(frm, "total_sanctioned_amount", "Mobile Money");
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
								status: ["!=", "Paid"],
								disbursed: 0,
							},
							fields: ["name", "employee", "employee_name", "rounded_total"],
						},
						callback: function (r) {
							if (r.message && r.message.length > 0) {
								let items = r.message.map((slip) => {
									return {
										reference_doctype: "Salary Slip",
										reference_name: slip.name,
										recipient_name: slip.employee_name,
										amount: slip.rounded_total,
										channel: "Bank Transfer", // Can be overwritten via backend auto-detect
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
