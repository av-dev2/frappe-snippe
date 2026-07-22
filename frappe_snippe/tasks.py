import frappe
from frappe.utils import now_datetime
from frappe_snippe.api.client import get_snippe_client

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
        fields=["name"]
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
                    response = client.payouts.get(item.snippe_reference)
                    
                    status_map = {
                        "pending": "Pending",
                        "successful": "Completed",
                        "failed": "Failed",
                        "reversed": "Reversed"
                    }
                    new_status = status_map.get(response.status, "Pending")
                    
                    if new_status != item.status:
                        item.db_set("status", new_status)
                        if new_status in ("Failed", "Reversed"):
                            item.db_set("failure_reason", getattr(response, "failure_reason", ""))
                        updated = True
                except Exception as e:
                    frappe.logger("frappe_snippe").error(f"Failed to reconcile payout {item.snippe_reference}: {str(e)}")
                    
        if updated:
            pd.update_status()
            
        pd.db_set("last_status_check", now_datetime())
