frappe.ui.form.on("Payment Collection", {
	setup(frm) {
		frm.set_query("mode_of_payment", () => ({ filters: { type: "Phone", enabled: 1 } }));
	},
});
