import frappe
from frappe import _
from frappe.model.document import Document

from frappe_snippe.api.webhooks import apply_event


class PaymentLog(Document):
	def process(self):
		"""Apply the event; a failure stays on the log for Reprocess instead of being lost."""
		frappe.db.savepoint("snippe_event")
		try:
			apply_event(self)
		except Exception:
			frappe.db.rollback(save_point="snippe_event")
			self.db_set({"processing_status": "Failed", "processing_error": frappe.get_traceback()})
			frappe.log_error(
				title="Snippe event processing failed",
				reference_doctype=self.doctype,
				reference_name=self.name,
			)
			return

		self.db_set({"processing_status": "Processed", "processing_error": None})

	@frappe.whitelist()
	def reprocess(self):
		self.check_permission("write")
		if self.processing_status == "Processed":
			frappe.throw(_("Payment Log {0} is already processed.").format(self.name))

		self.process()
