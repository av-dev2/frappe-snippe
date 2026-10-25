import typing

import frappe
from frappe import _
from frappe.model.document import Document
from snippe.types import BankCode

SNIPPE_BANK_CODES = typing.get_args(BankCode)


class PaymentDisbursementItem(Document):
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
