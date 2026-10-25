frappe.ui.form.on("Payment Collection", {
	setup(frm) {
		frm.set_query("mode_of_payment", () => ({ filters: { type: "Phone", enabled: 1 } }));
	},

	refresh(frm) {
		if (frm.doc.docstatus !== 1) return;

		if (frm.doc.status === "Pending" && frm.doc.payment_method === "Mobile Money") {
			frm.add_custom_button(__("Resend USSD Push"), () =>
				frm.call("resend_push").then(() =>
					frappe.show_alert({
						message: __("USSD push sent again."),
						indicator: "green",
					})
				)
			);
		}
		if (["Failed", "Expired", "Voided"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Retry"), async () => {
				const { message: name } = await frm.call("retry");
				frappe.set_route("Form", "Payment Collection", name);
			});
		}
	},
});
