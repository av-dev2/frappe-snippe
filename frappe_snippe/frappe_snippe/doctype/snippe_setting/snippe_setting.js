frappe.ui.form.on("Snippe Setting", {
	setup(frm) {
		frm.set_query("collection_account", (doc) => ({
			filters: {
				company: doc.company,
				account_type: ["in", ["Bank", "Cash"]],
				account_currency: "TZS",
				is_group: 0,
			},
		}));
		frm.set_query("fees_account", (doc) => ({
			filters: { company: doc.company, root_type: "Expense", is_group: 0 },
		}));
	},

	refresh(frm) {
		if (
			frm.is_new() ||
			frm.is_dirty() ||
			!(frm.doc.enable_collection || frm.doc.enable_payouts)
		) {
			return;
		}

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
