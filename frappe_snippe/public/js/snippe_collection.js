function add_snippe_collection_button(frm) {
	if (frm.doc.docstatus === 1 && frm.doc.outstanding_amount > 0) {
		frm.add_custom_button(
			__("Collect via Snippe"),
			function () {
				frappe.new_doc("Payment Collection", {
					company: frm.doc.company,
					customer: frm.doc.customer,
					references: [
						{
							reference_doctype: frm.doc.doctype,
							reference_name: frm.doc.name,
							allocated_amount: frm.doc.outstanding_amount,
						},
					],
				});
			},
			__("Create")
		);
	}
}

frappe.ui.form.on("Sales Invoice", {
	refresh: function (frm) {
		add_snippe_collection_button(frm);
	},
});

frappe.ui.form.on("Sales Order", {
	refresh: function (frm) {
		// For Sales Order, checking status or docstatus
		if (frm.doc.docstatus === 1 && frm.doc.per_billed < 100) {
			frm.add_custom_button(
				__("Collect via Snippe"),
				function () {
					frappe.new_doc("Payment Collection", {
						company: frm.doc.company,
						customer: frm.doc.customer,
						references: [
							{
								reference_doctype: frm.doc.doctype,
								reference_name: frm.doc.name,
								allocated_amount: frm.doc.grand_total - frm.doc.advance_paid,
							},
						],
					});
				},
				__("Create")
			);
		}
	},
});
