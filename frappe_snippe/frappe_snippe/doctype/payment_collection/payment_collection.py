import frappe
from frappe.model.document import Document
from frappe_snippe.api.client import get_snippe_client
import uuid
import json

class PaymentCollection(Document):
    def before_insert(self):
        if not self.idempotency_key:
            self.idempotency_key = str(uuid.uuid4())
            
        # calculate total amount
        self.total_amount = sum(row.allocated_amount for row in self.references)

    def on_submit(self):
        self.process_collection()
        
    def process_collection(self):
        client = get_snippe_client()
        
        # Phone number formatting if needed
        # Assuming phone_number is already correct format, or use a util.
        
        metadata = {
            "erpnext_doc": self.name,
            "company": self.company
        }
        
        response = None
        
        if self.payment_method == "Mobile Money":
            if not self.phone_number:
                frappe.throw(frappe._("Phone Number is required for Mobile Money."))
            response = client.payments.create(
                amount=self.total_amount,
                currency="TZS",
                payment_method="mno",
                phone_number=self.phone_number,
                description=f"Payment for {self.name}",
                metadata=metadata,
                idempotency_key=self.idempotency_key
            )
        elif self.payment_method == "Card":
            if not self.phone_number:
                frappe.throw(frappe._("Phone Number is required for Card payment creation."))
            response = client.payments.create(
                amount=self.total_amount,
                currency="TZS",
                payment_method="card",
                phone_number=self.phone_number,
                description=f"Payment for {self.name}",
                metadata=metadata,
                idempotency_key=self.idempotency_key
            )
        elif self.payment_method == "Dynamic QR":
            response = client.payments.create(
                amount=self.total_amount,
                currency="TZS",
                payment_method="qr",
                description=f"Payment for {self.name}",
                metadata=metadata,
                idempotency_key=self.idempotency_key
            )
        elif self.payment_method == "Hosted Session":
            response = client.sessions.create(
                amount=self.total_amount,
                currency="TZS",
                description=f"Payment for {self.name}",
                metadata=metadata,
                idempotency_key=self.idempotency_key
            )
            
        if response:
            self.db_set("raw_response", response.model_dump_json(indent=2))
            
            if self.payment_method == "Hosted Session":
                self.db_set("snippe_reference", response.id)
                self.db_set("checkout_url", response.url)
                self.db_set("payment_link_url", getattr(response, "short_url", None) or response.url)
                self.db_set("status", "Pending")
            else:
                self.db_set("snippe_reference", response.id)
                status_map = {
                    "pending": "Pending",
                    "successful": "Completed",
                    "failed": "Failed"
                }
                self.db_set("status", status_map.get(response.status, "Pending"))
                
                if self.payment_method == "Card":
                    self.db_set("payment_url", response.payment_url)
                elif self.payment_method == "Dynamic QR":
                    self.db_set("payment_qr_code", response.qr_code)
                    
        # Update references
        for row in self.references:
            row.db_set("status", "Pending")
