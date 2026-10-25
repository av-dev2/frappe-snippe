const SNIPPE_PAYMENT_METHOD =
	"frappe_snippe.frappe_snippe.doctype.payment_collection.payment_collection";
const PAYMENT_CHECK_SECONDS = 5;
const PRIMARY_ACTION_LABELS = {
	"Mobile Money": __("Send USSD Push"),
	Card: __("Create Card Payment"),
	"Dynamic QR": __("Show QR Code"),
};

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
		// Declared first: the Payment Method onchange can fire while the dialog is still being built.
		let dialog = null;
		dialog = new frappe.ui.Dialog({
			title: __("Make Payment"),
			fields: [
				...(frm.doctype === "Customer" ? [company_field] : []),
				{
					fieldname: "payment_method",
					fieldtype: "Select",
					label: __("Payment Method"),
					options: [
						{ value: "Mobile Money", label: __("Mobile Money (USSD push)") },
						{ value: "Card", label: __("Card") },
						{ value: "Dynamic QR", label: __("QR Code") },
					],
					default: "Mobile Money",
					reqd: 1,
					onchange: () =>
						dialog
							?.get_primary_btn()
							.text(PRIMARY_ACTION_LABELS[dialog.get_value("payment_method")]),
				},
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
			primary_action_label: PRIMARY_ACTION_LABELS["Mobile Money"],
			primary_action(values) {
				frappe
					.xcall(`${SNIPPE_PAYMENT_METHOD}.make_payment`, { ...args, ...values })
					.then((collection) => {
						dialog.hide();
						if (values.payment_method !== "Mobile Money") {
							show_payment_screen(frm, collection);
							return;
						}
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

// Card and QR payments happen on the customer's side, so the cashier waits here until Snippe confirms.
function show_payment_screen(frm, collection) {
	const dialog = new frappe.ui.Dialog({
		title: __("Waiting for Payment"),
		fields: [{ fieldname: "payment", fieldtype: "HTML" }],
		secondary_action_label: __("Close"),
		secondary_action: () => dialog.hide(),
	});
	const wrapper = dialog.fields_dict.payment.$wrapper;
	let timer = null;
	let is_rendered = false;
	// dialog.display follows the open and close animations, so it lags behind the dialog's real state.
	let is_closed = false;
	const check = () =>
		frappe
			.xcall(`${SNIPPE_PAYMENT_METHOD}.get_payment_progress`, { collection })
			.then((progress) => {
				if (is_closed) return;
				if (!is_rendered) {
					render_payment(wrapper, progress);
					is_rendered = true;
				}
				show_payment_status(wrapper, progress.status);
				if (progress.status === "Pending") return;

				clearInterval(timer);
				if (progress.status === "Completed") {
					is_closed = true;
					dialog.hide();
					frappe.show_alert({ message: __("Payment received"), indicator: "green" });
					frm.reload_doc();
				}
			});
	dialog.onhide = () => {
		is_closed = true;
		clearInterval(timer);
	};
	dialog.show();
	check();
	timer = setInterval(check, PAYMENT_CHECK_SECONDS * 1000);
}

function render_payment(wrapper, progress) {
	const $body = $('<div class="text-center"></div>').appendTo(wrapper);
	if (progress.payment_qr_code) {
		// Snippe sends the QR as a Base64 image, with or without its data: prefix.
		const qr_code = progress.payment_qr_code.startsWith("data:")
			? progress.payment_qr_code
			: `data:image/png;base64,${progress.payment_qr_code}`;
		$("<img>")
			.attr({ src: qr_code, alt: __("Payment QR code"), width: 240 })
			.appendTo($body);
		$("<p>")
			.text(__("Ask the customer to scan this code with their mobile money or banking app."))
			.appendTo($body);
	}
	if (progress.payment_url && progress.payment_url.startsWith("https://")) {
		$('<button class="btn btn-primary btn-sm"></button>')
			.text(__("Open Card Payment"))
			.on("click", () => window.open(progress.payment_url, "_blank", "noopener"))
			.appendTo($("<p>").appendTo($body));
		$("<img>")
			.attr({
				src: `data:image/svg+xml;base64,${progress.payment_url_qr_svg}`,
				alt: __("Card payment link"),
				width: 200,
			})
			.appendTo($body);
		$("<p>")
			.text(__("Or the customer scans this code to pay by card on their own phone."))
			.appendTo($body);
	}
	$('<p class="text-muted snippe-payment-status"></p>').appendTo($body);
}

function show_payment_status(wrapper, status) {
	wrapper
		.find(".snippe-payment-status")
		.text(
			status === "Pending"
				? __("Waiting for the customer to pay...")
				: __("Payment {0}. Take payment another way, or make a new payment.", [
						__(status).toLowerCase(),
				  ])
		);
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
