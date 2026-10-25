const SNIPPE_PAYMENT_METHOD =
	"frappe_snippe.frappe_snippe.doctype.payment_collection.payment_collection";

// Each company's Snippe Setting flags, sent at login by frappe_snippe.api.client.extend_bootinfo.
function get_collection_companies() {
	return Object.keys(frappe.boot.snippe_features || {}).filter(
		(company) => frappe.boot.snippe_features[company].enable_collection
	);
}

function has_snippe_collection(frm) {
	// A Customer belongs to no company, so a deposit needs any company that collects.
	if (frm.doctype === "Customer") return get_collection_companies().length > 0;
	return get_collection_companies().includes(frm.doc.company);
}

function can_pay_via_snippe(frm) {
	if (frm.is_new() || !has_snippe_collection(frm)) return false;
	if (frm.doctype === "Customer") return true;
	if (frm.doctype === "Loan") {
		return (
			frm.doc.docstatus === 1 &&
			frm.doc.applicant_type === "Customer" &&
			frm.doc.total_payment > frm.doc.total_amount_paid
		);
	}
	if (frm.doc.docstatus === 0) return true;
	return (
		frm.doctype === "Sales Invoice" &&
		frm.doc.docstatus === 1 &&
		frm.doc.outstanding_amount > 0
	);
}

function open_snippe_payment_dialog(frm, payment_row) {
	if (frm.is_dirty()) {
		frappe.msgprint(__("Save the document before taking payment."));
		return;
	}

	const args = { doctype: frm.doctype, name: frm.doc.name, payment_row: payment_row || null };
	frappe.xcall(`${SNIPPE_PAYMENT_METHOD}.get_payment_defaults`, args).then((defaults) => {
		const companies = get_collection_companies();
		const company_field = {
			fieldname: "company",
			fieldtype: "Link",
			options: "Company",
			label: __("Company"),
			reqd: 1,
			default: companies.length === 1 ? companies[0] : null,
			get_query: () => ({ filters: { name: ["in", companies] } }),
		};
		const dialog = new frappe.ui.Dialog({
			title: __("Make Payment"),
			fields: [
				...(frm.doctype === "Customer" ? [company_field] : []),
				{
					fieldname: "mode_of_payment",
					fieldtype: "Link",
					options: "Mode of Payment",
					label: __("Mode of Payment"),
					reqd: 1,
					read_only: payment_row ? 1 : 0,
					default: defaults.mode_of_payment,
					get_query: () => ({ filters: { type: "Phone", enabled: 1 } }),
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldname: "amount",
					fieldtype: "Currency",
					label: __("Amount"),
					reqd: 1,
					default: defaults.amount,
				},
				{
					fieldname: "phone_number",
					fieldtype: "Data",
					options: "Phone",
					label: __("Customer Phone"),
					reqd: 1,
					default: defaults.phone_number,
				},
			],
			primary_action_label: __("Send USSD Push"),
			primary_action(values) {
				frappe
					.xcall(`${SNIPPE_PAYMENT_METHOD}.make_payment`, { ...args, ...values })
					.then((collection) => {
						dialog.hide();
						frappe.show_alert({
							message: __(
								"USSD push sent with {0}. It is booked when the customer pays.",
								[collection]
							),
							indicator: "green",
						});
					});
			},
		});
		dialog.show();
	});
}

// Grid cells only show formatted values, so unpaid Phone rows get a Pay button through the formatter.
function show_row_pay_buttons(frm) {
	const grid = frm.fields_dict.payments?.grid;
	if (!grid) return;

	const can_pay = can_pay_via_snippe(frm) && frm.doc.docstatus === 0;
	let changed = false;
	for (const row of frm.doc.payments || []) {
		const value = can_pay && row.type === "Phone" && !row.reference_no ? row.name : "";
		changed ||= (row.make_payment || "") !== value;
		row.make_payment = value;
	}
	if (!changed) return;

	grid.update_docfield_property("make_payment", "formatter", (value) =>
		value
			? `<button class="btn btn-xs btn-default snippe-pay-row" data-row="${frappe.utils.escape_html(
					value
			  )}">${__("Pay")}</button>`
			: ""
	);
	// Capture the click before the grid turns the row into edit mode and redraws the button away.
	const wrapper = grid.wrapper[0];
	if (!wrapper.dataset.snippePay) {
		wrapper.dataset.snippePay = "1";
		wrapper.addEventListener(
			"click",
			(event) => {
				const button = event.target.closest(".snippe-pay-row");
				if (!button) return;
				event.stopPropagation();
				open_snippe_payment_dialog(frm, button.dataset.row);
			},
			true
		);
	}
	grid.refresh();
}

for (const doctype of ["Loan", "Customer"]) {
	frappe.ui.form.on(doctype, {
		refresh(frm) {
			if (can_pay_via_snippe(frm)) {
				frm.add_custom_button(__("Make Payment"), () => open_snippe_payment_dialog(frm));
			}
		},
	});
}

for (const doctype of ["Sales Invoice", "POS Invoice"]) {
	frappe.ui.form.on(doctype, {
		refresh(frm) {
			if (can_pay_via_snippe(frm)) {
				frm.add_custom_button(__("Make Payment"), () => open_snippe_payment_dialog(frm));
			}
			show_row_pay_buttons(frm);
		},
	});
}

frappe.ui.form.on("Sales Invoice Payment", {
	make_payment(frm, cdt, cdn) {
		if (can_pay_via_snippe(frm)) open_snippe_payment_dialog(frm, cdn);
	},
	type(frm) {
		show_row_pay_buttons(frm);
	},
});

frappe.ui.form.on("Sales Order", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1 || frm.doc.per_billed >= 100 || !has_snippe_collection(frm))
			return;

		frm.add_custom_button(
			__("Collect via Snippe"),
			() =>
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
				}),
			__("Create")
		);
	},
});
