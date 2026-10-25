import typing

import frappe
from erpnext.accounts.doctype.bank_account.bank_account import get_party_bank_account
from frappe import _
from frappe.model.document import Document
from snippe.types import BankCode

SNIPPE_BANK_CODES = typing.get_args(BankCode)
PARTY_PHONE_FIELDS = {"Employee": "cell_number", "Supplier": "mobile_no", "Customer": "mobile_no"}


class PaymentDisbursementItem(Document):
	def set_party_bank_details(self):
		"""Fill a blank bank and account from the payee's default Bank Account."""
		if not self.reference_name or self.bank or self.bank_account:
			return

		bank_account = get_party_bank_account(*self.get_party())
		if not bank_account:
			return

		self.bank, self.bank_account = frappe.db.get_value(
			"Bank Account", bank_account, ["bank", "bank_account_no"]
		)
		# Link fetches ran before validate, so fetch the code of the bank set here.
		self.bank_code = self.bank_code or frappe.db.get_value("Bank", self.bank, "bank_code")

	def set_party_phone(self):
		"""Fill a blank phone from the payee's own record."""
		if self.phone_number or not self.reference_name:
			return

		party_type, party = self.get_party()
		if party_type in PARTY_PHONE_FIELDS:
			self.phone_number = frappe.db.get_value(party_type, party, PARTY_PHONE_FIELDS[party_type])

	def get_party(self) -> tuple[str, str]:
		if self.reference_doctype in ("Purchase Invoice", "Purchase Order"):
			return "Supplier", frappe.db.get_value(self.reference_doctype, self.reference_name, "supplier")
		if self.reference_doctype == "Sales Invoice":
			return "Customer", frappe.db.get_value("Sales Invoice", self.reference_name, "customer")
		if self.reference_doctype == "Loan":
			return frappe.db.get_value("Loan", self.reference_name, ["applicant_type", "applicant"])
		return "Employee", frappe.db.get_value(self.reference_doctype, self.reference_name, "employee")

	def validate_bank_details(self):
		if not (self.bank_code and self.bank_account):
			frappe.throw(
				_("Row #{0}: Set the Bank Code and Bank Account to pay {1} by bank transfer.").format(
					self.idx, self.recipient_name or self.reference_name
				),
				title=_("Missing Bank Details"),
			)
		if self.bank_code not in SNIPPE_BANK_CODES:
			frappe.throw(
				_("Row #{0}: Snippe does not support the Bank Code {1}.").format(self.idx, self.bank_code),
				title=_("Unsupported Bank Code"),
			)
