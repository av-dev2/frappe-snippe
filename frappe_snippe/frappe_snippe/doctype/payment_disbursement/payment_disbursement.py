import uuid

import frappe
from frappe.model.document import Document

from frappe_snippe.api.client import get_snippe_client


class PaymentDisbursement(Document):
    def before_insert(self):
        self.calculate_totals()

    def calculate_totals(self):
        self.total_amount = sum(row.amount for row in self.items)

        # In a real scenario, we might want to calculate total_fee by calling an endpoint,
        # but the SDK might not expose fee estimation yet, so we'll leave it 0 or get it later.
        self.total_fee = 0
        self.total_to_deduct = self.total_amount + self.total_fee

        # Ensure idempotency keys
        for row in self.items:
            if not row.idempotency_key:
                row.idempotency_key = str(uuid.uuid4())

    def on_submit(self):
        # We process this asynchronously because bulk disbursements can take time
        self.db_set("status", "Processing")
        frappe.enqueue(
            "frappe_snippe.frappe_snippe.doctype.payment_disbursement.payment_disbursement.process_disbursement_queue",
            payment_disbursement=self.name,
            queue="short",
        )


@frappe.whitelist()
def process_disbursement_queue(payment_disbursement: str):
    doc = frappe.get_doc("Payment Disbursement", payment_disbursement)
    client = get_snippe_client()

    settings = frappe.get_single("Snippe Settings")
    default_narration = settings.default_narration or "Payment"

    for row in doc.items:
        if row.status != "Pending":
            continue

        metadata = {"erpnext_doc": doc.name, "row_id": row.name}

        try:
            if row.channel == "Mobile Money":
                response = client.payouts.create(
                    amount=row.amount,
                    currency="TZS",
                    payment_method="mno",
                    phone_number=row.phone_number,
                    description=doc.narration or default_narration,
                    metadata=metadata,
                    idempotency_key=row.idempotency_key,
                )
            elif row.channel == "Bank Transfer":
                response = client.payouts.create(
                    amount=row.amount,
                    currency="TZS",
                    payment_method="bank",
                    bank_code=row.bank_code,
                    account_number=row.bank_account,
                    account_name=row.recipient_name,
                    description=doc.narration or default_narration,
                    metadata=metadata,
                    idempotency_key=row.idempotency_key,
                )

            row.db_set("snippe_reference", response.id)
            status_map = {
                "pending": "Pending",
                "successful": "Completed",
                "failed": "Failed",
                "reversed": "Reversed",
            }
            row.db_set("status", status_map.get(response.status, "Pending"))

        except Exception as e:
            row.db_set("status", "Failed")
            row.db_set("failure_reason", str(e))

    doc.update_status()


def update_status(self):
    total = len(self.items)
    completed = sum(1 for row in self.items if row.status == "Completed")
    failed = sum(1 for row in self.items if row.status in ("Failed", "Reversed"))

    if completed == total:
        self.db_set("status", "Completed")
    elif failed == total:
        self.db_set("status", "Failed")
    elif completed > 0 or failed > 0:
        self.db_set("status", "Partially Completed")
    else:
        self.db_set("status", "Processing")


PaymentDisbursement.update_status = update_status
