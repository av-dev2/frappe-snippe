frappe.ui.form.on("Payment Log", {
	refresh(frm) {
		if (frm.doc.processing_status === "Processed") return;

		frm.add_custom_button(__("Reprocess"), async () => {
			await frm.call("reprocess");
			await frm.reload_doc();
		});
	},
});
