import re
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import frappe
from erpnext import get_company_currency
from frappe import _
from frappe.utils import cint, flt, get_system_timezone
from snippe import AuthenticationError, ConflictError, RateLimitError, Snippe, ValidationError

SNIPPE_CURRENCY = "TZS"
LIST_PAGE_SIZE = 100
# Two lists of 20 pages stay inside Snippe's 60 requests a minute.
LIST_MAX_PAGES = 20
MINIMUM_AMOUNT = 500
IDEMPOTENCY_KEY_LENGTH = 30
RATE_LIMIT_WAIT_SECONDS = 60
# Background jobs wait out Snippe's 60 requests a minute; user requests fail at once instead.
BACKGROUND_RATE_LIMIT_WAITS = 2

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


class TooManyRecordsError(frappe.ValidationError):
	pass


class SnippeClient(Snippe):
	"""Snippe SDK client plus the Payment Sessions endpoints, which SDK 0.1.4 lacks.

	Snippe validates session amounts and expiry itself; its 400 replies raise ValidationError.
	"""

	def __init__(self, *args, rate_limit_waits: int = 0, **kwargs):
		super().__init__(*args, **kwargs)
		self.rate_limit_waits = rate_limit_waits

	def _handle_response(self, response):
		# A resent request keeps its Idempotency-Key, so a payout cannot be sent twice.
		for _wait in range(self.rate_limit_waits):
			if response.status_code != 429:
				break
			time.sleep(cint(response.headers.get("X-Ratelimit-Reset")) or RATE_LIMIT_WAIT_SECONDS)
			response = self._client.send(response.request)

		return super()._handle_response(response)

	def get_payment_data(self, reference):
		"""Raw payment status; unlike List Payments and the webhook, it has no settlement fees."""
		return self._handle_response(self._client.get(f"/payments/{quote(reference, safe='')}"))

	def create_session(
		self,
		amount=None,
		*,
		currency=SNIPPE_CURRENCY,
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

	def list_payment_data(self, limit=100, offset=0) -> dict:
		"""One page of payments, newest first, as Snippe sends them: items, total, limit, offset."""
		return self._handle_response(self._client.get("/payments", params={"limit": limit, "offset": offset}))

	def list_payout_data(self, limit=100, offset=0) -> dict:
		"""One page of payouts, newest first, as Snippe sends them: items, total, limit, offset."""
		return self._handle_response(self._client.get("/payouts", params={"limit": limit, "offset": offset}))

	def trigger_push(self, reference):
		"""Send the USSD push of a pending mobile payment to the customer's phone again."""
		return self._handle_response(self._client.post(f"/payments/{quote(reference, safe='')}/push"))

	def cancel_session(self, reference):
		return self._handle_response(self._client.post(f"/sessions/{quote(reference, safe='')}/cancel"))


def get_snippe_setting(company: str):
	try:
		return frappe.get_cached_doc("Snippe Setting", company)
	except frappe.DoesNotExistError:
		frappe.clear_last_message()
		frappe.throw(
			_("Create a Snippe Setting for Company {0} first.").format(company),
			title=_("Company Not Set Up for Snippe"),
		)


def get_snippe_client(company: str, rate_limit_waits: int = 0) -> SnippeClient:
	"""Client for the company's Snippe account; the SDK supplies the API base URL."""
	api_key = get_snippe_setting(company).get_password("api_key", raise_exception=False)
	if not api_key:
		frappe.throw(_("Set the API Key in Snippe Setting {0}.").format(company))

	return SnippeClient(api_key=api_key, rate_limit_waits=rate_limit_waits)


def extend_bootinfo(bootinfo):
	"""Each company's Snippe features, so forms show only the matching buttons without a server call."""
	bootinfo.snippe_features = {
		setting.name: setting
		for setting in frappe.get_all(
			"Snippe Setting", fields=["name", "enable_collection", "enable_payouts"]
		)
	}


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


def validate_snippe_currency(doctype: str, names: list[str]):
	"""Snippe moves TZS only, so stop documents in any other currency."""
	currency_field = "currency" if frappe.get_meta(doctype).has_field("currency") else "company"
	for document in frappe.get_all(doctype, filters={"name": ("in", names)}, fields=["name", currency_field]):
		currency = document.get("currency") or get_company_currency(document.company)
		if currency != SNIPPE_CURRENCY:
			frappe.throw(
				_("{0} {1} is in {2}. Snippe pays and collects in {3} only.").format(
					_(doctype), document.name, currency, SNIPPE_CURRENCY
				),
				title=_("Unsupported Currency"),
			)


def validate_rows_currency(rows):
	"""Check each referenced document's currency with one query per DocType."""
	names_by_doctype = defaultdict(list)
	for row in rows:
		# validate runs before the mandatory check, so empty links are left to it.
		if row.reference_name:
			names_by_doctype[row.reference_doctype].append(row.reference_name)

	for doctype, names in names_by_doctype.items():
		validate_snippe_currency(doctype, names)


def validate_snippe_account_currency(account: str):
	currency = frappe.get_cached_value("Account", account, "account_currency")
	if currency != SNIPPE_CURRENCY:
		frappe.throw(
			_("Account {0} is in {1}. Snippe pays and collects in {2} only.").format(
				account, currency, SNIPPE_CURRENCY
			),
			title=_("Unsupported Currency"),
		)


def list_since(list_page, from_date: date) -> dict:
	"""Records of a newest-first Snippe list by reference, paged until they pass from_date.

	Each record gets its local date; List Payments records also carry their settlement fees.
	"""
	records = {}
	for page in range(LIST_MAX_PAGES):
		data = list_page(limit=LIST_PAGE_SIZE, offset=page * LIST_PAGE_SIZE)
		items = data.get("items") or []
		for item in items:
			item["date"] = to_local_date(item["created_at"])
			records[item["reference"]] = item

		is_last_page = (page + 1) * LIST_PAGE_SIZE >= cint(data.get("total"))
		if not items or items[-1]["date"] < from_date or is_last_page:
			return records

	raise TooManyRecordsError(
		_("Snippe has more than {0} records since {1}.").format(
			LIST_MAX_PAGES * LIST_PAGE_SIZE, frappe.format(from_date, "Date")
		)
	)


def to_local_date(timestamp: str) -> date:
	moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
	return moment.astimezone(ZoneInfo(get_system_timezone())).date()


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


@contextmanager
def snippe_errors_as_messages():
	"""Show Snippe's rejection to the user and stop the submit or cancel."""
	try:
		yield
	except (ValidationError, ConflictError, RateLimitError, AuthenticationError) as error:
		frappe.throw(
			_("Snippe rejected the request: {0}").format(error.message), title=_("Snippe Request Rejected")
		)
