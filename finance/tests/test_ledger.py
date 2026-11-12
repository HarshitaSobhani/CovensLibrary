"""Phase 3b: ledger, receipts, split payments, backdating, immutability (PostgreSQL)."""

import uuid
from datetime import date
from unittest import mock

from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TestCase

from core.errors import RuleViolation
from core.models import IdSequence
from finance import services as fs
from finance.models import FinancialTransaction, PaymentEntry, ReceiptDocument
from finance.services import Entry, Item

from .helpers import FLOW, K, make_member, make_owner, sub_item, tok_item

TODAY = date(2026, 11, 11)


def post(member, owner, items, today=TODAY, **kw):
    with transaction.atomic():
        return fs.post_items(member=member, items=items, actor=owner, today=today, **kw)


class Base(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member = make_member(self.owner)

    def code(self, items, today=TODAY, **kw):
        with self.assertRaises(RuleViolation) as ctx:
            post(self.member, self.owner, items, today, **kw)
        return ctx.exception.code


class ReceiptNumberingTests(Base):
    def test_four_separate_sequences_start_at_one_each(self):
        adj = Item(K.ADJUSTMENT, FLOW.NONE, 100, TODAY, reason="fix", label="Adjustment")
        refund = Item(K.TOKEN_REFUND, FLOW.OUT, 50000, TODAY, [Entry("cash", 50000)], label="R")
        got = post(self.member, self.owner, [sub_item(), tok_item(), refund, adj])
        self.assertEqual(
            [t.receipt_no for t in got.transactions],
            ["SUB-2026-000001", "TOK-2026-000001", "REF-2026-000001", "ADJ-2026-000001"],
        )
        again = post(self.member, self.owner, [sub_item(), tok_item()])
        self.assertEqual(
            [t.receipt_no for t in again.transactions], ["SUB-2026-000002", "TOK-2026-000002"]
        )

    def test_year_is_the_issuance_year_never_the_backdated_payment_year(self):
        items = [sub_item(day=date(2026, 12, 31))]
        got = post(
            self.member,
            self.owner,
            items,
            today=date(2027, 1, 5),
            backdate_reason="entered late",
            backdate_confirmed=True,
        )
        txn = got.transactions[0]
        self.assertEqual(txn.receipt_no, "SUB-2027-000001")
        self.assertEqual((txn.receipt_year, txn.receipt_issued_on), (2027, date(2027, 1, 5)))
        self.assertEqual(txn.effective_date, date(2026, 12, 31))

    def test_counter_restarts_each_calendar_year_and_is_independent_per_year(self):
        post(self.member, self.owner, [sub_item(day=date(2026, 12, 31))], today=date(2026, 12, 31))
        a = post(self.member, self.owner, [sub_item(day=date(2027, 1, 1))], today=date(2027, 1, 1))
        b = post(self.member, self.owner, [sub_item(day=date(2027, 1, 1))], today=date(2027, 1, 1))
        self.assertEqual(a.transactions[0].receipt_no, "SUB-2027-000001")
        self.assertEqual(b.transactions[0].receipt_no, "SUB-2027-000002")
        self.assertEqual(
            IdSequence.objects.get(name="receipt_subscription", year=2026).last_value, 1
        )

    def test_every_transaction_gets_a_distinct_internal_uuid(self):
        got = post(self.member, self.owner, [sub_item(), tok_item()])
        ids = [t.internal_txn_id for t in got.transactions]
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(all(isinstance(i, uuid.UUID) for i in ids))

    def test_combined_registration_printout_lists_both_receipts(self):
        got = post(self.member, self.owner, [sub_item(), tok_item()], document_kind="registration")
        doc = got.document
        self.assertEqual(doc.kind, "registration")
        self.assertEqual(
            [line["receipt_no"] for line in doc.snapshot["lines"]],
            ["SUB-2026-000001", "TOK-2026-000001"],
        )
        self.assertTrue(all(t.receipt_document_id == doc.pk for t in got.transactions))
        self.assertEqual(doc.snapshot["membership_no"], self.member.membership_no)
        self.assertNotIn(
            self.member.full_name, str(doc.snapshot)
        )  # name shown live (anonymisation)

    def test_forfeiture_and_reversal_have_no_receipt_number(self):
        forfeiture = Item(K.TOKEN_FORFEITURE, FLOW.NONE, 50000, TODAY, label="F")
        got = post(self.member, self.owner, [forfeiture])
        txn = got.transactions[0]
        self.assertEqual((txn.receipt_no, txn.receipt_sequence, txn.receipt_year), ("", "", None))
        self.assertIsNone(got.document)
        self.assertFalse(IdSequence.objects.filter(name__startswith="receipt_").exists())

    def test_failed_posting_consumes_no_receipt_number(self):
        before = IdSequence.objects.count()
        self.assertEqual(self.code([sub_item(entries=[Entry("cash", 79999)])]), "underpaid")
        self.assertEqual(IdSequence.objects.count(), before)
        got = post(self.member, self.owner, [sub_item()])
        self.assertEqual(got.transactions[0].receipt_no, "SUB-2026-000001")  # no gap


class SplitPaymentTests(Base):
    def test_cash_and_upi_settle_one_obligation(self):
        entries = [Entry("cash", 30000), Entry("upi", 50000, reference="UPI-9")]
        txn = post(self.member, self.owner, [sub_item(entries=entries)]).transactions[0]
        self.assertEqual(txn.amount_paise, 80000)
        self.assertEqual(
            list(txn.entries.values_list("method", "amount_paise", "reference")),
            [("cash", 30000, ""), ("upi", 50000, "UPI-9")],
        )

    def test_partial_payment_records_nothing(self):
        for amount in (0, 1, 79999):
            with self.subTest(amount=amount):
                entries = [Entry("cash", amount)] if amount else []
                expected = "payment_required" if not amount else "underpaid"
                self.assertEqual(self.code([sub_item(entries=entries)]), expected)
        self.assertEqual(FinancialTransaction.objects.count(), 0)
        self.assertEqual(ReceiptDocument.objects.count(), 0)
        self.assertEqual(PaymentEntry.objects.count(), 0)

    def test_overpayment_and_bad_entries_are_rejected(self):
        self.assertEqual(self.code([sub_item(entries=[Entry("cash", 80001)])]), "overpaid")
        self.assertEqual(
            self.code([sub_item(entries=[Entry("cash", 90000), Entry("upi", -10000)])]),
            "amount_invalid",
        )
        self.assertEqual(self.code([sub_item(entries=[Entry("card", 80000)])]), "method_invalid")
        self.assertEqual(self.code([sub_item(entries=[Entry("cash", 800.0)])]), "amount_invalid")
        self.assertEqual(self.code([sub_item(entries=[Entry("cash", True)])]), "amount_invalid")
        long_ref = Entry("cash", 80000, reference="x" * 121)
        self.assertEqual(self.code([sub_item(entries=[long_ref])]), "reference_too_long")

    def test_amount_must_be_a_positive_whole_number_of_paise(self):
        for bad, code in ((0, "amount_invalid"), (-5, "amount_invalid"), (10.5, "amount_invalid")):
            item = Item(K.SUBSCRIPTION_PAYMENT, FLOW.IN, bad, TODAY, [Entry("cash", 100)])
            self.assertEqual(self.code([item]), code)

    def test_cash_free_records_cannot_carry_payments(self):
        item = Item(K.TOKEN_FORFEITURE, FLOW.NONE, 50000, TODAY, [Entry("cash", 50000)])
        self.assertEqual(self.code([item]), "unexpected_payment")

    def test_arithmetic_is_exact_for_many_small_splits(self):
        entries = [Entry("cash", 1)] * 299 + [Entry("upi", 79701)]
        txn = post(self.member, self.owner, [sub_item(entries=entries)]).transactions[0]
        self.assertEqual(sum(e.amount_paise for e in txn.entries.all()), 80000)


class BackdatingTests(Base):
    def test_future_dates_are_rejected(self):
        self.assertEqual(self.code([sub_item(day=date(2026, 11, 12))]), "future_date")
        future_payment = [Entry("cash", 80000, paid_on=date(2026, 11, 12))]
        self.assertEqual(self.code([sub_item(entries=future_payment)]), "future_date")

    def test_past_dates_need_a_reason_and_extra_confirmation(self):
        past = sub_item(day=date(2026, 11, 1))
        self.assertEqual(self.code([past]), "backdate_reason_required")
        self.assertEqual(self.code([past], backdate_reason="  "), "backdate_reason_required")
        self.assertEqual(
            self.code([past], backdate_reason="Paid at the counter"),
            "backdate_confirmation_required",
        )
        txn = post(
            self.member,
            self.owner,
            [past],
            backdate_reason="Paid at the counter",
            backdate_confirmed=True,
        ).transactions[0]
        self.assertTrue(txn.backdated)
        self.assertTrue(txn.backdate_financial_confirmed)
        self.assertEqual(txn.backdate_reason, "Paid at the counter")

    def test_a_past_payment_date_alone_counts_as_backdating(self):
        entries = [Entry("cash", 80000, paid_on=date(2026, 11, 10))]
        self.assertEqual(self.code([sub_item(entries=entries)]), "backdate_reason_required")

    def test_todays_date_is_not_backdated(self):
        txn = post(self.member, self.owner, [sub_item()]).transactions[0]
        self.assertFalse(txn.backdated)
        self.assertEqual(txn.backdate_reason, "")


class IdempotencyTests(Base):
    def test_same_request_key_posts_once(self):
        key = uuid.uuid4()
        post(self.member, self.owner, [sub_item(request_key=key)])
        self.assertEqual(self.code([sub_item(request_key=key)]), "duplicate_submission")
        self.assertEqual(FinancialTransaction.objects.count(), 1)
        self.assertEqual(IdSequence.objects.get(name="receipt_subscription").last_value, 1)

    def test_derived_keys_are_stable_and_distinct_per_kind(self):
        key = uuid.uuid4()
        self.assertEqual(fs.derived_key(key, "a"), fs.derived_key(key, "a"))
        self.assertNotEqual(fs.derived_key(key, "a"), fs.derived_key(key, "b"))
        self.assertIsNone(fs.derived_key(None, "a"))


class AtomicityTests(Base):
    def test_a_failure_after_numbering_rolls_everything_back(self):
        real = PaymentEntry.objects.bulk_create
        calls = []

        def boom(*a, **k):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("disk full")
            return real(*a, **k)

        with mock.patch.object(PaymentEntry.objects, "bulk_create", side_effect=boom):
            with self.assertRaises(RuntimeError):
                post(self.member, self.owner, [sub_item(), tok_item()])
        self.assertEqual(FinancialTransaction.objects.count(), 0)
        self.assertEqual(ReceiptDocument.objects.count(), 0)
        self.assertEqual(PaymentEntry.objects.count(), 0)
        self.assertEqual(IdSequence.objects.filter(name__startswith="receipt_").count(), 0)

    def test_posting_outside_a_transaction_is_refused(self):
        # TestCase wraps tests in a transaction; simulate the guard directly
        with mock.patch.object(connection, "in_atomic_block", False):
            with self.assertRaises(RuntimeError):
                fs.post_items(member=self.member, items=[sub_item()], actor=self.owner, today=TODAY)


class DatabaseProtectionTests(Base):
    """Raw SQL as the owner role: the triggers and CHECKs bind everyone."""

    def setUp(self):
        super().setUp()
        self.txn = post(
            self.member, self.owner, [sub_item(), tok_item()], document_kind="registration"
        ).transactions[0]

    def execute(self, sql, params=None):
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(sql, params or [])

    def test_ledger_rows_cannot_be_updated_or_deleted(self):
        for table in (
            "finance_financialtransaction",
            "finance_paymententry",
            "finance_receiptdocument",
        ):
            for sql in (f"UPDATE {table} SET id = id", f"DELETE FROM {table}"):
                with self.subTest(sql=sql), self.assertRaisesMessage(DatabaseError, "append-only"):
                    self.execute(sql)

    def test_orm_updates_and_deletes_are_blocked_too(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            FinancialTransaction.objects.filter(pk=self.txn.pk).update(reason="edited")
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.txn.entries.all().delete()

    def _insert_txn(self, **overrides):
        values = dict(
            internal_txn_id=str(uuid.uuid4()),
            member_id=self.member.pk,
            kind="subscription_payment",
            cash_flow="in",
            amount_paise=100,
            effective_date="2026-11-11",
            receipt_sequence="subscription",
            receipt_issued_on="2026-11-11",
            receipt_year=2026,
            receipt_no="SUB-2026-000099",
            receipt_document_id=self.txn.receipt_document_id,
            reason="",
            backdated=False,
            backdate_reason="",
            backdate_financial_confirmed=False,
            recorded_by_id=self.owner.pk,
        )
        values.update(overrides)
        cols = ", ".join(values)
        marks = ", ".join(["%s"] * len(values))
        self.execute(
            f"INSERT INTO finance_financialtransaction ({cols}, processed_at) "
            f"VALUES ({marks}, now())",
            list(values.values()),
        )

    def test_check_constraints_reject_invalid_rows(self):
        bad = {
            "unknown kind": dict(kind="subscription_refund"),
            "fee refund shaped as out-flow": dict(cash_flow="out"),
            "zero amount": dict(amount_paise=0),
            "bad cash flow": dict(cash_flow="bad"),
            "receipt prefix mismatch": dict(receipt_no="TOK-2026-000099"),
            "receipt missing": dict(receipt_no="", receipt_sequence=""),
            "reason-less adjustment": dict(
                kind="adjustment",
                receipt_sequence="adjustment",
                receipt_no="ADJ-2026-000099",
                reason="",
            ),
            "backdated without reason": dict(backdated=True),
            "reversal without original": dict(
                kind="reversal",
                receipt_sequence="",
                receipt_no="",
                receipt_issued_on=None,
                receipt_year=None,
                receipt_document_id=None,
                reason="x",
            ),
            "forfeiture with a receipt": dict(kind="token_forfeiture", cash_flow="none"),
        }
        for name, overrides in bad.items():
            with self.subTest(name), self.assertRaises(IntegrityError):
                self._insert_txn(**overrides)

    def test_receipt_numbers_are_unique(self):
        with self.assertRaises(IntegrityError):
            self._insert_txn(receipt_no=self.txn.receipt_no)
        with self.assertRaises(IntegrityError):
            self._insert_txn(
                internal_txn_id=str(self.txn.internal_txn_id), receipt_no="SUB-2026-000098"
            )

    def test_deferred_trigger_enforces_payment_sum_at_commit(self):
        with self.assertRaisesMessage(DatabaseError, "must equal the transaction amount"):
            with transaction.atomic():
                self._insert_txn()  # cash transaction with no payment entries
                with connection.cursor() as cur:
                    cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        with self.assertRaisesMessage(DatabaseError, "moves no cash"):
            with transaction.atomic():
                self._insert_txn(
                    kind="token_forfeiture",
                    cash_flow="none",
                    receipt_sequence="",
                    receipt_no="",
                    receipt_issued_on=None,
                    receipt_year=None,
                    receipt_document_id=None,
                )
                with connection.cursor() as cur:
                    cur.execute(
                        "INSERT INTO finance_paymententry (transaction_id, method, amount_paise, "
                        "paid_on, reference, recorded_at) SELECT max(id), 'cash', 1, "
                        "'2026-11-11', '', now() FROM finance_financialtransaction"
                    )
                    cur.execute("SET CONSTRAINTS ALL IMMEDIATE")

    def test_entries_must_be_positive_and_a_known_method(self):
        for method, amount in (("card", 10), ("cash", 0), ("cash", -4)):
            with self.subTest(method=method, amount=amount), self.assertRaises(IntegrityError):
                self.execute(
                    "INSERT INTO finance_paymententry (transaction_id, method, amount_paise, "
                    "paid_on, reference, recorded_at) VALUES (%s, %s, %s, '2026-11-11', '', now())",
                    [self.txn.pk, method, amount],
                )

    def test_no_fee_refund_exists_anywhere(self):
        kinds = {k for k, _ in FinancialTransaction.Kind.choices}
        self.assertEqual(
            kinds,
            {
                "subscription_payment",
                "token_collection",
                "token_refund",
                "token_forfeiture",
                "adjustment",
                "reversal",
            },
        )
        self.assertFalse(any("fee_refund" in k or "subscription_refund" in k for k in kinds))
