from collections import defaultdict

import frappe

# reference doctype -> (party type, party field); a Loan names its own applicant type
REFERENCE_PARTIES = {
	"Purchase Invoice": ("Supplier", "supplier"),
	"Purchase Order": ("Supplier", "supplier"),
	"Sales Invoice": ("Customer", "customer"),
	"Salary Slip": ("Employee", "employee"),
	"Employee Advance": ("Employee", "employee"),
	"Expense Claim": ("Employee", "employee"),
	"Loan": (None, "applicant"),
}
PARTY_PHONE_FIELDS = {"Employee": "cell_number", "Supplier": "mobile_no", "Customer": "mobile_no"}


def fill_payee_details(rows: list):
	"""Fill blank phones and bank details from each payee's record, with a few queries for all rows."""
	rows = [row for row in rows if row.reference_name]
	phone_rows = [row for row in rows if row.channel == "Mobile Money" and not row.phone_number]
	bank_rows = [row for row in rows if row.channel != "Mobile Money" and not (row.bank or row.bank_account)]
	if not (phone_rows or bank_rows):
		return

	parties = get_parties(phone_rows + bank_rows)
	set_phones(phone_rows, parties)
	set_bank_details(bank_rows, parties)


def get_parties(rows: list) -> dict:
	"""{(reference doctype, reference name): (party type, party)}, one query per reference DocType."""
	names = defaultdict(set)
	for row in rows:
		names[row.reference_doctype].add(row.reference_name)

	parties = {}
	for doctype, references in names.items():
		party_type, party_field = REFERENCE_PARTIES[doctype]
		fields = ["name", party_field] + ([] if party_type else ["applicant_type"])
		for reference in frappe.get_all(doctype, filters={"name": ("in", list(references))}, fields=fields):
			parties[(doctype, reference.name)] = (
				party_type or reference.applicant_type,
				reference[party_field],
			)
	return parties


def set_phones(rows: list, parties: dict):
	party_names = defaultdict(set)
	for row in rows:
		party_type, party = parties.get((row.reference_doctype, row.reference_name), (None, None))
		if party_type in PARTY_PHONE_FIELDS:
			party_names[party_type].add(party)

	phones = {}
	for party_type, names in party_names.items():
		field = PARTY_PHONE_FIELDS[party_type]
		for record in frappe.get_all(
			party_type, filters={"name": ("in", list(names))}, fields=["name", field]
		):
			phones[(party_type, record.name)] = record[field]

	for row in rows:
		row.phone_number = phones.get(parties.get((row.reference_doctype, row.reference_name)))


def set_bank_details(rows: list, parties: dict):
	"""Fill a blank bank and account from the payee's default Bank Account, as ERPNext picks it."""
	row_parties = [parties.get((row.reference_doctype, row.reference_name)) for row in rows]
	party_names = {party for _party_type, party in filter(None, row_parties)}
	if not party_names:
		return

	accounts = {
		(account.party_type, account.party): account
		for account in frappe.get_all(
			"Bank Account",
			filters={"party": ("in", list(party_names)), "is_default": 1, "disabled": 0},
			fields=["party_type", "party", "bank", "bank_account_no"],
			# A party has a default per company; the newest wins, as ERPNext's get_party_bank_account picks it.
			order_by="modified",
		)
	}
	if not accounts:
		return

	bank_codes = dict(
		frappe.get_all(
			"Bank",
			filters={"name": ("in", [account.bank for account in accounts.values()])},
			fields=["name", "bank_code"],
			as_list=True,
		)
	)

	for row, party in zip(rows, row_parties, strict=True):
		account = accounts.get(party)
		if account:
			row.bank, row.bank_account = account.bank, account.bank_account_no
			row.bank_code = row.bank_code or bank_codes.get(account.bank)
