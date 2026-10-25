// Each company's Snippe Setting flags, sent at login by frappe_snippe.api.client.extend_bootinfo.
function when_snippe_enabled(frm, feature, add_button) {
	if (frm.doc.docstatus === 1 && frappe.boot.snippe_features?.[frm.doc.company]?.[feature]) {
		add_button();
	}
}

function add_snippe_disbursement_button(frm, amount, default_channel) {
	when_snippe_enabled(frm, "enable_payouts", () => {
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
	});
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

// Payroll Entry pulls the Salary Slips that still have pay left from the server
frappe.ui.form.on("Payroll Entry", {
	refresh: function (frm) {
		when_snippe_enabled(frm, "enable_payouts", () => {
			frm.add_custom_button(
				__("Disburse via Snippe"),
				function () {
					frappe
						.xcall(
							"frappe_snippe.frappe_snippe.doctype.payment_disbursement.payment_disbursement.get_salary_slip_rows",
							{ payroll_entry: frm.doc.name }
						)
						.then((items) => {
							if (!items.length) {
								frappe.msgprint(
									__("No unpaid salary slips found for this payroll.")
								);
								return;
							}
							frappe.new_doc("Payment Disbursement", {
								company: frm.doc.company,
								disbursement_type: "Salary",
								payroll_entry: frm.doc.name,
								items: items,
							});
						});
				},
				__("Create")
			);
		});
	},
});
