from unittest.mock import MagicMock, patch

import frappe
from frappe.model.document import Document
from frappe.tests.utils import FrappeTestCase
from snippe import Customer, NotFoundError, Payment, Payout, RateLimitError, ServerError, ValidationError

from frappe_snippe import tasks as reconcile_tasks
from frappe_snippe.api.client import (
	BACKGROUND_RATE_LIMIT_WAITS,
	new_idempotency_key,
	normalize_phone,
	to_snippe_amount,
)
from frappe_snippe.frappe_snippe.doctype.payment_disbursement import payees
from frappe_snippe.frappe_snippe.doctype.payment_disbursement.payment_disbursement import (
	get_payout_channel,
	get_salary_slip_rows,
	process_disbursement_queue,
)
from frappe_snippe.tasks import reconcile_pending_disbursements
from frappe_snippe.tests.test_webhooks import set_args

COLLECTION_MODULE = "frappe_snippe.frappe_snippe.doctype.payment_collection.payment_collection"
DISBURSEMENT_MODULE = "frappe_snippe.frappe_snippe.doctype.payment_disbursement.payment_disbursement"


def make_payment(status="pending"):
	return Payment.from_dict(
		{
			"reference": "pay_ref",
			"status": status,
			"amount": {"value": 1500, "currency": "TZS"},
			"payment_type": "mobile",
			"payment_url": "https://checkout.example/pay",
			"payment_qr_code": "qr-image",
		}
	)


def make_payout(status="pending", failure_reason=None):
	return Payout.from_dict({"reference": "po_ref", "status": status, "failure_reason": failure_reason})


def make_collection(payment_method="Mobile Money"):
	return frappe.get_doc(
		{
			"doctype": "Payment Collection",
			"name": "PAY-COL-TEST",
			"company": "Test Company",
			"customer": "CUST-0001",
			"customer_name": "Asha Juma Mrisho",
			"payment_method": payment_method,
			"phone_number": "255712345678",
			"total_amount": 1500.4,
			"idempotency_key": "key-1",
			"references": [{"reference_doctype": "Sales Invoice", "allocated_amount": 1500.4}],
		}
	)


def make_disbursement(items, **values):
	return frappe.get_doc(
		{
			"doctype": "Payment Disbursement",
			"name": "PAY-DIS-TEST",
			"narration": "Salary",
			"items": items,
			**values,
		}
	)


def mobile_item(**values):
	return {
		"name": "row-1",
		"channel": "Mobile Money",
		"recipient_name": "Asha Juma",
		"phone_number": "0712345678",
		"amount": 5000,
		"status": "Pending",
		"idempotency_key": "row-key",
		**values,
	}


class TestSnippeHelpers(FrappeTestCase):
	def test_amount_is_rounded_to_whole_tzs(self):
		self.assertEqual(to_snippe_amount(1500.4), 1500)
		self.assertIsInstance(to_snippe_amount("2000"), int)

	def test_amount_below_minimum_is_rejected(self):
		self.assertEqual(to_snippe_amount(500), 500)
		with self.assertRaises(frappe.ValidationError):
			to_snippe_amount(499)

	def test_idempotency_key_fits_snippe_limit(self):
		keys = {new_idempotency_key() for _index in range(50)}
		self.assertEqual(len(keys), 50)
		self.assertTrue(all(len(key) == 30 for key in keys))

	def test_phone_numbers_are_normalised(self):
		for phone in ("0712345678", "712345678", "+255 712 345 678", "+255-712345678", "255712345678"):
			self.assertEqual(normalize_phone(phone), "255712345678")
		self.assertEqual(normalize_phone("0612345678"), "255612345678")

	def test_invalid_phone_numbers_are_rejected(self):
		for phone in ("", None, "12345", "0212345678", "254712345678"):
			with self.assertRaises(frappe.ValidationError):
				normalize_phone(phone)


@patch.object(Document, "db_set")
class TestPaymentCollectionSdk(FrappeTestCase):
	def run_collection(self, collection, client):
		client.__enter__.return_value = client
		# Customer and card URL lookups are covered in test_collection_customer.
		setting = frappe._dict(get_card_return_urls=lambda: {"redirect_url": "https://shop.example/paid"})
		with (
			patch(f"{COLLECTION_MODULE}.get_snippe_client", return_value=client),
			patch(f"{COLLECTION_MODULE}.get_snippe_setting", return_value=setting),
			patch.object(collection, "get_snippe_customer", return_value=Customer("Asha", "Juma Mrisho")),
		):
			collection.process_collection()

	def test_mobile_payment_uses_sdk_call(self, db_set):
		client = MagicMock()
		client.create_mobile_payment.return_value = make_payment("completed")
		self.run_collection(make_collection(), client)

		kwargs = client.create_mobile_payment.call_args.kwargs
		self.assertEqual(kwargs["amount"], 1500)
		self.assertEqual(kwargs["phone_number"], "255712345678")
		self.assertEqual(kwargs["idempotency_key"], "key-1")
		self.assertTrue(kwargs["webhook_url"].endswith("/api/method/frappe_snippe.api.webhooks.handle"))
		self.assertEqual((kwargs["customer"].firstname, kwargs["customer"].lastname), ("Asha", "Juma Mrisho"))
		values = db_set.call_args_list[0].args[0]
		self.assertEqual(values["snippe_reference"], "pay_ref")
		self.assertEqual(values["status"], "Completed")

	def test_card_and_qr_payments_use_their_sdk_calls(self, db_set):
		client = MagicMock()
		client.create_card_payment.return_value = make_payment()
		client.create_qr_payment.return_value = make_payment("expired")
		self.run_collection(make_collection("Card"), client)
		self.run_collection(make_collection("Dynamic QR"), client)

		self.assertTrue(client.create_card_payment.call_args.kwargs["redirect_url"].startswith("http"))
		client.create_qr_payment.assert_called_once()
		self.assertEqual(db_set.call_args_list[-2].args[0]["status"], "Expired")

	def test_sdk_error_blocks_submit(self, db_set):
		client = MagicMock()
		client.create_mobile_payment.side_effect = ValidationError("Invalid phone", 400)
		with self.assertRaisesRegex(frappe.ValidationError, "Invalid phone"):
			self.run_collection(make_collection(), client)
		db_set.assert_not_called()

	def test_validate_normalises_phone_and_shortens_legacy_key(self, db_set):
		collection = make_collection()
		collection.phone_number = "0712 345 678"
		collection.idempotency_key = "9b2f6c1e-4d1a-4f5e-9a63-0c7b5d1e2f3a"
		with patch(f"{COLLECTION_MODULE}.get_snippe_setting"):
			collection.validate()
		self.assertEqual(collection.phone_number, "255712345678")
		self.assertLessEqual(len(collection.idempotency_key), 30)


@patch.object(Document, "db_set")
class TestPaymentDisbursementSdk(FrappeTestCase):
	def run_queue(self, disbursement, client):
		client.__enter__.return_value = client
		with (
			patch(f"{DISBURSEMENT_MODULE}.frappe.get_doc", return_value=disbursement),
			patch(f"{DISBURSEMENT_MODULE}.get_snippe_setting", return_value=frappe._dict()),
			patch(f"{DISBURSEMENT_MODULE}.get_snippe_client", return_value=client) as get_client,
			patch.object(frappe.db, "commit") as commit,
			# Accounting is covered in test_disbursement_accounting.
			patch.object(disbursement, "book_item"),
		):
			process_disbursement_queue("PAY-DIS-TEST")
		return get_client, commit

	def test_mobile_and_bank_payouts_use_sdk_calls(self, db_set):
		disbursement = make_disbursement(
			[
				mobile_item(),
				mobile_item(
					name="row-2", channel="Bank Transfer", bank_code="CRDB", bank_account="0211049375"
				),
			]
		)
		client = MagicMock()
		client.create_mobile_payout.return_value = make_payout("completed")
		client.create_bank_payout.return_value = make_payout()
		self.run_queue(disbursement, client)

		mobile_kwargs = client.create_mobile_payout.call_args.kwargs
		self.assertEqual(mobile_kwargs["recipient_phone"], "255712345678")
		self.assertEqual(mobile_kwargs["amount"], 5000)
		self.assertTrue(
			mobile_kwargs["webhook_url"].endswith("/api/method/frappe_snippe.api.webhooks.handle")
		)
		bank_kwargs = client.create_bank_payout.call_args.kwargs
		self.assertEqual(
			(bank_kwargs["recipient_bank"], bank_kwargs["recipient_account"]), ("CRDB", "0211049375")
		)
		self.assertEqual(db_set.call_args_list[0].args[0], {"snippe_reference": "po_ref", "fee_amount": 0})
		self.assertEqual(db_set.call_args_list[1].args[0], {"status": "Completed"})

	def test_validation_error_fails_only_that_row(self, db_set):
		disbursement = make_disbursement([mobile_item(), mobile_item(name="row-2")])
		client = MagicMock()
		client.create_mobile_payout.side_effect = [ValidationError("Bad account", 400), make_payout()]
		self.run_queue(disbursement, client)

		self.assertEqual(
			db_set.call_args_list[0].args[0],
			{"status": "Failed", "failure_reason": "Bad account", "fee_amount": 0},
		)
		self.assertEqual(client.create_mobile_payout.call_count, 2)

	def test_batch_client_waits_out_the_rate_limit(self, db_set):
		get_client, _commit = self.run_queue(
			make_disbursement([mobile_item()], company="Test Company"), MagicMock()
		)
		get_client.assert_called_once_with("Test Company", BACKGROUND_RATE_LIMIT_WAITS)

	def test_each_sent_or_failed_row_is_committed(self, db_set):
		disbursement = make_disbursement([mobile_item(), mobile_item(name="row-2")])
		client = MagicMock()
		client.create_mobile_payout.side_effect = [make_payout(), ValidationError("Bad account", 400)]
		_get_client, commit = self.run_queue(disbursement, client)
		self.assertEqual(commit.call_count, 2)

	def test_lasting_rate_limit_stops_batch_and_keeps_rows_pending(self, db_set):
		disbursement = make_disbursement([mobile_item(), mobile_item(name="row-2")])
		client = MagicMock()
		client.create_mobile_payout.side_effect = RateLimitError("Too many requests", 429)
		with patch(f"{DISBURSEMENT_MODULE}.frappe.log_error") as log_error:
			self.run_queue(disbursement, client)

		log_error.assert_called_once()
		self.assertEqual(client.create_mobile_payout.call_count, 1)
		row_updates = [
			call.args[0]
			for call in db_set.call_args_list
			if isinstance(call.args[0], dict) and "status" in call.args[0]
		]
		self.assertEqual(row_updates, [])

	def test_server_error_keeps_references_of_sent_rows(self, db_set):
		disbursement = make_disbursement(
			[mobile_item(), mobile_item(name="row-2"), mobile_item(name="row-3")]
		)
		client = MagicMock()
		client.create_mobile_payout.side_effect = [make_payout(), ServerError("Gateway down", 503)]
		with patch(f"{DISBURSEMENT_MODULE}.frappe.log_error") as log_error:
			self.run_queue(disbursement, client)

		log_error.assert_called_once()
		self.assertEqual(client.create_mobile_payout.call_count, 2)
		self.assertEqual(db_set.call_args_list[0].args[0], {"snippe_reference": "po_ref", "fee_amount": 0})

	def test_payout_queue_is_not_callable_over_the_api(self, db_set):
		with self.assertRaises(frappe.PermissionError):
			frappe.is_whitelisted(process_disbursement_queue)

	def test_sent_rows_are_not_sent_again(self, db_set):
		disbursement = make_disbursement([mobile_item(snippe_reference="po_old")])
		client = MagicMock()
		self.run_queue(disbursement, client)
		client.create_mobile_payout.assert_not_called()

	def test_validate_rejects_amount_below_minimum(self, db_set):
		with self.assertRaises(frappe.ValidationError):
			make_disbursement([mobile_item(amount=499)]).validate()


@patch.object(Document, "db_set")
class TestReconcilePayouts(FrappeTestCase):
	def run_reconcile(self, disbursement, client):
		client.__enter__.return_value = client
		with (
			patch("frappe_snippe.tasks.frappe.get_all", return_value=["PAY-DIS-TEST"]),
			patch("frappe_snippe.tasks.frappe.get_doc", return_value=disbursement),
			patch("frappe_snippe.tasks.get_snippe_client", return_value=client),
			patch.object(disbursement, "book_item"),
		):
			reconcile_pending_disbursements()

	def test_payout_status_is_read_with_get_payout(self, db_set):
		disbursement = make_disbursement(
			[mobile_item(snippe_reference="po_1"), mobile_item(name="row-2", snippe_reference="po_2")]
		)
		client = MagicMock()
		client.get_payout.side_effect = [make_payout("reversed", "Wallet closed"), make_payout("completed")]
		self.run_reconcile(disbursement, client)

		self.assertEqual(client.get_payout.call_args_list[0].args, ("po_1",))
		self.assertIn(({"status": "Reversed", "failure_reason": "Wallet closed"},), set_args(db_set))
		self.assertIn(({"status": "Completed"},), set_args(db_set))

	def test_sdk_error_skips_only_that_payout(self, db_set):
		disbursement = make_disbursement(
			[mobile_item(snippe_reference="po_1"), mobile_item(name="row-2", snippe_reference="po_2")]
		)
		client = MagicMock()
		client.get_payout.side_effect = [NotFoundError("Missing", 404), make_payout("failed", "Rejected")]
		with patch("frappe_snippe.tasks.frappe.log_error") as log_error:
			self.run_reconcile(disbursement, client)

		log_error.assert_called_once()
		self.assertEqual(log_error.call_args.kwargs["reference_name"], "PAY-DIS-TEST")
		self.assertIn("po_1", log_error.call_args.kwargs["message"])
		self.assertEqual(client.get_payout.call_count, 2)
		self.assertIn(
			({"status": "Failed", "failure_reason": "Rejected"},),
			[call.args for call in db_set.call_args_list],
		)


@patch.object(Document, "db_set")
class TestPayoutResume(FrappeTestCase):
	def hours_ago(self, hours):
		return frappe.utils.add_to_date(None, hours=-hours)

	def test_submit_queues_one_resumable_batch(self, db_set):
		disbursement = make_disbursement([mobile_item()])
		with patch(f"{DISBURSEMENT_MODULE}.frappe.enqueue") as enqueue:
			disbursement.on_submit()

		self.assertEqual(db_set.call_args.args[0]["status"], "Processing")
		self.assertIsNotNone(db_set.call_args.args[0]["queued_on"])
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(
			(kwargs["queue"], kwargs["job_id"], kwargs["deduplicate"], kwargs["enqueue_after_commit"]),
			("long", "snippe_payouts::PAY-DIS-TEST", True, True),
		)

	def test_unsent_rows_are_resent_within_the_idempotency_window(self, db_set):
		disbursement = make_disbursement([mobile_item()], queued_on=self.hours_ago(5))
		with patch.object(disbursement, "queue_payouts") as queue_payouts:
			disbursement.resume_unsent_payouts()

		queue_payouts.assert_called_once()
		db_set.assert_not_called()

	def test_unsent_rows_fail_once_a_resend_could_pay_twice(self, db_set):
		disbursement = make_disbursement(
			[mobile_item(), mobile_item(name="row-2", snippe_reference="po_sent")],
			queued_on=self.hours_ago(21),
		)
		with patch.object(disbursement, "queue_payouts") as queue_payouts:
			disbursement.resume_unsent_payouts()

		queue_payouts.assert_not_called()
		failed = [args[0] for args in set_args(db_set) if isinstance(args[0], dict)]
		self.assertEqual(len(failed), 1)
		self.assertEqual(failed[0]["status"], "Failed")
		self.assertIn("Check the Snippe dashboard", failed[0]["failure_reason"])

	def test_hourly_job_resumes_each_stopped_disbursement(self, db_set):
		disbursement = MagicMock()
		with (
			patch("frappe_snippe.tasks.frappe.get_all", return_value=["PAY-DIS-TEST"]) as get_all,
			patch("frappe_snippe.tasks.frappe.get_doc", return_value=disbursement),
		):
			reconcile_tasks.resume_unsent_payouts()

		disbursement.resume_unsent_payouts.assert_called_once()
		self.assertIn(["queued_on", "is", "set"], get_all.call_args.kwargs["filters"])


class TestPayrollDisbursementRows(FrappeTestCase):
	def test_only_salary_slips_with_pay_left_become_rows(self):
		slips = [
			frappe._dict(name="SAL-1", employee="EMP-1", employee_name="Asha"),
			frappe._dict(name="SAL-2", employee="EMP-2", employee_name="Juma"),
		]
		employees = [
			frappe._dict(name=name, payout_channel=None, bank_ac_no="0150", cell_number=None)
			for name in ("EMP-1", "EMP-2")
		]
		with (
			patch(f"{DISBURSEMENT_MODULE}.frappe.has_permission") as has_permission,
			patch(f"{DISBURSEMENT_MODULE}.frappe.get_all", side_effect=[slips, employees]),
			patch(
				f"{DISBURSEMENT_MODULE}.get_amounts_left_to_pay", return_value={"SAL-1": 0, "SAL-2": 450000}
			) as left_to_pay,
		):
			rows = get_salary_slip_rows("HR-PRUN-0001")

		has_permission.assert_called_once_with("Payroll Entry", doc="HR-PRUN-0001", throw=True)
		left_to_pay.assert_called_once_with("Salary Slip", ["SAL-1", "SAL-2"], "")
		self.assertEqual(
			rows,
			[
				{
					"reference_doctype": "Salary Slip",
					"reference_name": "SAL-2",
					"recipient_name": "Juma",
					"amount": 450000,
					"channel": "Bank Transfer",
					"phone_number": None,
				}
			],
		)

	def test_payroll_without_submitted_slips_has_no_rows(self):
		with (
			patch(f"{DISBURSEMENT_MODULE}.frappe.has_permission"),
			patch(f"{DISBURSEMENT_MODULE}.frappe.get_all", return_value=[]),
		):
			self.assertEqual(get_salary_slip_rows("HR-PRUN-0001"), [])


@patch.object(Document, "db_set")
class TestPayoutPhonesAndStatus(FrappeTestCase):
	def test_mobile_money_row_without_phone_blocks_only_submit(self, db_set):
		disbursement = make_disbursement(
			[mobile_item(phone_number=None), mobile_item(name="row-2", idx=2, phone_number=None)]
		)
		with self.assertRaisesRegex(frappe.ValidationError, "Mobile Money rows 1, 2"):
			disbursement.before_submit()

	def test_blank_phone_is_taken_from_the_payee(self, db_set):
		row = frappe.get_doc(
			{
				"doctype": "Payment Disbursement Item",
				"reference_doctype": "Expense Claim",
				"reference_name": "EC-1",
			}
		)
		row.channel = "Mobile Money"
		with (
			patch.object(
				payees, "get_parties", return_value={("Expense Claim", "EC-1"): ("Employee", "EMP-1")}
			),
			patch.object(
				payees.frappe, "get_all", return_value=[frappe._dict(name="EMP-1", cell_number="0712345678")]
			) as get_all,
		):
			payees.fill_payee_details([row])

		self.assertEqual(get_all.call_args.args[0], "Employee")
		self.assertEqual(row.phone_number, "0712345678")

	def test_payroll_channel_follows_the_employee(self, db_set):
		cases = [
			(
				frappe._dict(payout_channel="Mobile Money", bank_ac_no="0150", cell_number=None),
				"Mobile Money",
			),
			(frappe._dict(payout_channel=None, bank_ac_no=None, cell_number="0712"), "Mobile Money"),
			(frappe._dict(payout_channel=None, bank_ac_no="0150", cell_number="0712"), "Bank Transfer"),
		]
		for employee, channel in cases:
			self.assertEqual(get_payout_channel(employee), channel, employee)

	def test_owner_is_told_once_when_the_batch_ends(self, db_set):
		disbursement = make_disbursement([mobile_item(status="Completed")], status="Processing")
		with patch(f"{DISBURSEMENT_MODULE}.notify_owner") as notify_owner:
			disbursement.update_status()
			disbursement.status = "Completed"
			disbursement.update_status()

		db_set.assert_called_once_with("status", "Completed", notify=True)
		notify_owner.assert_called_once()
