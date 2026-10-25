frappe.ui.form.on("Payment Disbursement", {
	refresh(frm) {
		const has_failed_rows = frm.doc.items.some((row) =>
			["Failed", "Reversed"].includes(row.status)
		);
		if (frm.doc.docstatus === 1 && has_failed_rows) {
			frm.add_custom_button(__("Resend Failed Rows"), async () => {
				const { message: name } = await frm.call("make_retry_disbursement");
				frappe.set_route("Form", "Payment Disbursement", name);
			});
		}

		if (frm.doc.docstatus !== 0 || frm.is_new()) return;

		frm.add_custom_button(__("Check Fees & Balance"), async () => {
			if (frm.is_dirty()) await frm.save();
			const { message: preview } = await frm.call("check_fees_and_balance");
			await frm.reload_doc();
			show_fees_and_balance(preview);
		});
	},
});

function show_fees_and_balance(preview) {
	const currency = frappe.utils.escape_html(preview.currency);
	const money = (value) => format_currency(value, currency);
	const rows = preview.rows
		.map(
			(row) => `<tr>
				<td>${row.idx}</td>
				<td>${frappe.utils.escape_html(row.recipient_name || "")}</td>
				<td class="text-right">${money(row.amount)}</td>
				<td class="text-right">${money(row.fee_amount)}</td>
			</tr>`
		)
		.join("");
	const verdict = preview.is_balance_enough
		? `<span class="indicator-pill green">${__("Balance is enough to submit")}</span>`
		: `<span class="indicator-pill red">${__("Balance is too low to submit")}</span>`;

	frappe.msgprint({
		title: __("Snippe Fees & Balance"),
		wide: true,
		message: `
			<table class="table table-bordered table-condensed">
				<thead><tr>
					<th>#</th><th>${__("Recipient")}</th>
					<th class="text-right">${__("Amount")}</th>
					<th class="text-right">${__("Fee")}</th>
				</tr></thead>
				<tbody>${rows}</tbody>
			</table>
			<table class="table table-condensed">
				<tr><td>${__("Total Amount")}</td><td class="text-right">${money(preview.total_amount)}</td></tr>
				<tr><td>${__("Total Fee")}</td><td class="text-right">${money(preview.total_fee)}</td></tr>
				<tr><th>${__("Total to Deduct")}</th><th class="text-right">${money(
			preview.total_to_deduct
		)}</th></tr>
				<tr><td>${__("Available Snippe Balance")}</td><td class="text-right">${money(
			preview.available_balance
		)}</td></tr>
			</table>
			${verdict}`,
	});
}
