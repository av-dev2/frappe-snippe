frappe.ui.form.on("Snippe Setting", {
	setup(frm) {
		frm.set_query("wallet_account", (doc) => ({
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

	onload(frm) {
		show_balance(frm);
	},

	after_save(frm) {
		show_balance(frm);
	},

	refresh(frm) {
		if (frm.is_new() || !(frm.doc.enable_collection || frm.doc.enable_payouts)) {
			return;
		}

		frm.add_custom_button(
			__("Record Top-up"),
			() => record_wallet_transfer(frm, "Top-up"),
			__("Actions")
		);
		frm.add_custom_button(
			__("Record Withdrawal"),
			() => record_wallet_transfer(frm, "Withdrawal"),
			__("Actions")
		);
	},
});

function show_balance(frm) {
	if (frm.is_new() || !(frm.doc.enable_collection || frm.doc.enable_payouts)) {
		frm.dashboard.clear_headline();
		return;
	}

	frm.call("get_balance").then(({ message: balance }) => {
		const currency = frappe.utils.escape_html(balance.currency);
		const message = __("Snippe balance: {0} available of {1}. Wallet Account: {2}", [
			format_currency(balance.available_balance, currency),
			format_currency(balance.balance, currency),
			format_currency(balance.wallet_balance, currency),
		]);
		frm.dashboard.set_headline_alert(
			balance.difference
				? `${message}. ${__("Difference: {0}", [
						format_currency(balance.difference, currency),
				  ])}`
				: message,
			balance.difference ? "red" : "green"
		);
	});
}

function record_wallet_transfer(frm, transfer_type) {
	const is_top_up = transfer_type === "Top-up";
	const dialog = new frappe.ui.Dialog({
		title: is_top_up ? __("Record Top-up") : __("Record Withdrawal"),
		fields: [
			{
				fieldname: "bank_account",
				label: is_top_up ? __("Paid From") : __("Paid To"),
				fieldtype: "Link",
				options: "Account",
				reqd: 1,
				get_query: () => ({
					filters: {
						company: frm.doc.company,
						account_type: ["in", ["Bank", "Cash"]],
						is_group: 0,
						name: ["!=", frm.doc.wallet_account],
					},
				}),
			},
			{ fieldname: "amount", label: __("Amount"), fieldtype: "Currency", reqd: 1 },
			{
				fieldname: "fee",
				label: __("Fee"),
				fieldtype: "Currency",
				// Snippe charges 1,500 TZS for each withdrawal to a bank or mobile account.
				default: is_top_up ? 0 : 1500,
			},
			{
				fieldname: "posting_date",
				label: __("Posting Date"),
				fieldtype: "Date",
				default: frappe.datetime.get_today(),
				reqd: 1,
			},
			{ fieldname: "reference", label: __("Bank Reference"), fieldtype: "Data", reqd: 1 },
		],
		primary_action_label: __("Record"),
		primary_action(values) {
			frm.call({
				method: "record_wallet_transfer",
				args: { transfer_type, ...values },
				freeze: true,
			}).then(({ message: journal_entry }) => {
				dialog.hide();
				frappe.show_alert({
					message: __("Journal Entry {0} recorded", [journal_entry]),
					indicator: "green",
				});
				show_balance(frm);
			});
		},
	});
	dialog.show();
}
