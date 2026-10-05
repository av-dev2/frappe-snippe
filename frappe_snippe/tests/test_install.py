from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_snippe.install import after_app_install
from frappe_snippe.patches.custom_fields.create_custom_fields import create_fields_from_json


class TestInstall(FrappeTestCase):
    def test_custom_fields_skip_missing_doctype(self):
        field = {"dt": "Missing Snippe DocType", "fieldname": "disbursed", "fieldtype": "Check"}
        create_fields_from_json([field])
        self.assertFalse(frappe.db.exists("Custom Field", {"dt": field["dt"]}))

    def test_optional_app_install_creates_customizations(self):
        with patch("frappe_snippe.install.create_customizations") as create_customizations:
            after_app_install("crm")
            create_customizations.assert_not_called()
            after_app_install("hrms")
            create_customizations.assert_called_once()
