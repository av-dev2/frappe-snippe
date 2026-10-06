import re
import uuid

import frappe
from frappe import _
from frappe.utils import flt
from snippe import Snippe

MINIMUM_AMOUNT = 500
IDEMPOTENCY_KEY_LENGTH = 30

PAYMENT_STATUS = {
	"pending": "Pending",
	"completed": "Completed",
	"failed": "Failed",
	"expired": "Expired",
	"voided": "Voided",
}
PAYOUT_STATUS = {
	"pending": "Pending",
	"completed": "Completed",
	"failed": "Failed",
	"reversed": "Reversed",
}


def get_snippe_client():
	settings = frappe.get_single("Snippe Settings")
	if not settings.enabled:
		frappe.throw(_("Snippe integration is not enabled."))

	if not settings.api_key:
		frappe.throw(_("Snippe API Key is missing in Settings."))

	api_url = settings.api_url or ""
	return Snippe(api_key=settings.get_password("api_key"), base_url=api_url)


def new_idempotency_key():
	return uuid.uuid4().hex[:IDEMPOTENCY_KEY_LENGTH]


def is_valid_idempotency_key(key):
	return bool(key) and len(key) <= IDEMPOTENCY_KEY_LENGTH


def to_snippe_amount(amount):
	"""Return the amount as whole TZS, rejecting amounts below the Snippe minimum."""
	whole_amount = int(flt(amount, 0))
	if whole_amount < MINIMUM_AMOUNT:
		frappe.throw(_("Snippe requires an amount of at least {0} TZS.").format(MINIMUM_AMOUNT))
	return whole_amount


def normalize_phone(phone):
	"""Return a Tanzanian mobile number as 255XXXXXXXXX."""
	digits = re.sub(r"\D", "", phone or "")
	if len(digits) == 10 and digits.startswith("0"):
		digits = "255" + digits[1:]
	elif len(digits) == 9:
		digits = "255" + digits

	if not re.fullmatch(r"255[67]\d{8}", digits):
		frappe.throw(_("{0} is not a valid Tanzanian mobile number.").format(phone))
	return digits
