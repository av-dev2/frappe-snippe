import frappe

from frappe_snippe.patches.bank import create_snippe_banks
from frappe_snippe.patches.custom_fields import create_custom_fields
from frappe_snippe.patches.property_setter import create_property_setters

OPTIONAL_APPS = ("hrms", "lending")


def after_install():
	create_customizations()
	create_snippe_banks.execute()


def after_app_install(app_name):
	if app_name in OPTIONAL_APPS:
		create_customizations()


def create_customizations():
	"""Custom fields only land on DocTypes that exist, so rerun when an optional app arrives."""
	create_custom_fields.execute()
	create_property_setters.execute()
