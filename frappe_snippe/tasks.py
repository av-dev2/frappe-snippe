import frappe
from frappe.utils import now_datetime
from snippe import AuthenticationError, ConflictError, NotFoundError, RateLimitError, ValidationError

from frappe_snippe.api.client import PAYOUT_STATUS, get_snippe_client


def hourly():
	reconcile_pending_disbursements()


def reconcile_pending_disbursements():
	"""
	Check pending payment disbursements and sync their statuses if they've been
	stuck in Processing/Pending for too long.
	"""
	pending_docs = frappe.get_all(
		"Payment Disbursement",
		filters={"status": ("in", ["Processing", "Partially Completed"])},
		fields=["name"],
	)

	if not pending_docs:
		return

	client = get_snippe_client()

	for doc in pending_docs:
		pd = frappe.get_doc("Payment Disbursement", doc.name)
		updated = False

		for item in pd.items:
			if item.status == "Pending" and item.snippe_reference:
				try:
					response = client.get_payout(item.snippe_reference)
				except (
					NotFoundError,
					ValidationError,
					ConflictError,
					RateLimitError,
					AuthenticationError,
				) as e:
					frappe.log_error(
						title="Snippe payout reconcile failed",
						message=f"Payout {item.snippe_reference}: {e.message}",
						reference_doctype="Payment Disbursement",
						reference_name=pd.name,
					)
					continue

				new_status = PAYOUT_STATUS.get(response.status, "Pending")
				if new_status != item.status:
					item.db_set("status", new_status)
					if new_status in ("Failed", "Reversed"):
						item.db_set("failure_reason", response.failure_reason or "")
					updated = True

		if updated:
			pd.update_status()

		pd.db_set("last_status_check", now_datetime())
