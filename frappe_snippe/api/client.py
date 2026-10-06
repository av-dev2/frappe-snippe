import re
import uuid
from urllib.parse import quote

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
SESSION_STATUS = {
	"pending": "Pending",
	"active": "Pending",
	"completed": "Completed",
	"expired": "Expired",
	"cancelled": "Voided",
}


class SnippeClient(Snippe):
	"""Snippe SDK client plus the Payment Sessions endpoints, which SDK 0.1.4 lacks.

	Snippe validates session amounts and expiry itself; its 400 replies raise ValidationError.
	"""

	def create_session(
		self,
		amount=None,
		*,
		currency="TZS",
		allowed_methods=None,
		customer=None,
		redirect_url=None,
		webhook_url=None,
		description=None,
		metadata=None,
		expires_in=None,
		line_items=None,
		allow_custom_amount=False,
		min_amount=None,
		max_amount=None,
		display=None,
		idempotency_key=None,
	):
		payload = {
			"amount": amount,
			"currency": currency,
			"allowed_methods": allowed_methods,
			"customer": customer,
			"redirect_url": redirect_url,
			"webhook_url": webhook_url,
			"description": description,
			"metadata": metadata,
			"expires_in": expires_in,
			"line_items": line_items,
			"allow_custom_amount": allow_custom_amount or None,
			"min_amount": min_amount,
			"max_amount": max_amount,
			"display": display,
		}
		headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
		response = self._client.post(
			"/sessions",
			json={key: value for key, value in payload.items() if value is not None},
			headers=headers,
		)
		return self._handle_response(response)

	def get_session(self, reference):
		return self._handle_response(self._client.get(f"/sessions/{quote(reference, safe='')}"))

	def list_sessions(self, limit=20, offset=0, status=None):
		params = {"limit": limit, "offset": offset, "status": status}
		response = self._client.get(
			"/sessions", params={key: value for key, value in params.items() if value}
		)
		self._handle_response(response)
		body = response.json()
		return {"sessions": body["data"], **body.get("meta", {})}

	def cancel_session(self, reference):
		return self._handle_response(self._client.post(f"/sessions/{quote(reference, safe='')}/cancel"))


def get_snippe_client():
	settings = frappe.get_single("Snippe Settings")
	if not settings.enabled:
		frappe.throw(_("Snippe integration is not enabled."))

	if not settings.api_key:
		frappe.throw(_("Snippe API Key is missing in Settings."))

	api_url = settings.api_url or ""
	return SnippeClient(api_key=settings.get_password("api_key"), base_url=api_url)


def new_idempotency_key():
	return uuid.uuid4().hex[:IDEMPOTENCY_KEY_LENGTH]


def is_valid_idempotency_key(key):
	return bool(key) and len(key) <= IDEMPOTENCY_KEY_LENGTH


def whole_tzs(amount):
	return int(flt(amount, 0))


def to_snippe_amount(amount):
	"""Return the amount as whole TZS, rejecting amounts below the Snippe minimum."""
	whole_amount = whole_tzs(amount)
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
