import os

import frappe

BANKS_FILE = os.path.join(os.path.dirname(__file__), "snippe_banks.json")


def execute():
	"""Create a Bank for each Snippe bank code that has no Bank yet."""
	existing = frappe.get_all("Bank", fields=["name", "swift_number", "bank_code"])
	codes = {bank.bank_code for bank in existing}
	names = {bank.name.lower() for bank in existing}
	# SWIFT is unique on Bank, so a Bank with this SWIFT under another name is the same bank.
	swift_numbers = {bank.swift_number for bank in existing if bank.swift_number}

	for bank in frappe.get_file_json(BANKS_FILE):
		if (
			bank["bank_code"] in codes
			or bank["bank_name"].lower() in names
			or bank["swift_number"] in swift_numbers
		):
			continue
		frappe.get_doc({"doctype": "Bank", **bank}).insert(ignore_permissions=True)
