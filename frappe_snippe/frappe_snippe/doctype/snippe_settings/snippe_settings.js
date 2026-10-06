frappe.ui.form.on("Snippe Settings", {
	setup(frm) {
		frm.set_query("snippe_fees_account", () => ({
			filters: { root_type: "Expense", is_group: 0 },
		}));
	},

	refresh(frm) {
		if (!frm.doc.enabled || frm.is_dirty()) return;

		frm.call("get_balance").then(({ message: balance }) => {
			const currency = frappe.utils.escape_html(balance.currency);
			frm.dashboard.set_headline_alert(
				__("Snippe balance: {0} available of {1}", [
					format_currency(balance.available_balance, currency),
					format_currency(balance.balance, currency),
				])
			);
		});
	},
});
