"""Phase 3c: database constraints and triggers, exercised with raw SQL (as the owner role)."""

from datetime import date

from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TestCase

from core.errors import RuleViolation
from finance import tokens
from finance.models import FinancialTransaction, TokenLedgerEntry
from finance.services import Entry
from memberships import services as ms

from .helpers import TODAY, forfeit, make_owner, register, renew


def execute(sql, params=None):
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(sql, params or [])


class SubscriptionConstraintTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.result = register(self.owner)
        self.member = self.result.member
        self.sub = self.result.subscription

    def insert_sub(self, starts, expires, txn_id=None, cancelled=False):
        txn_id = txn_id or self.sub.payment_txn_id
        execute(
            "INSERT INTO memberships_membersubscription (member_id, token_cycle_id, plan_id, "
            "plan_name_snapshot, duration_months_snapshot, price_paise_snapshot, starts_on, "
            "expires_on, payment_txn_id, cancelled_at, cancel_reason, created_at) "
            "VALUES (%s, %s, %s, 'p', 6, 80000, %s, %s, %s, "
            + ("now(), 'r'" if cancelled else "NULL, ''")
            + ", now())",
            [self.member.pk, self.sub.token_cycle_id, self.sub.plan_id, starts, expires, txn_id],
        )

    def _other_txn(self):
        return renew(self.owner, self.member, today=date(2027, 1, 1)).transactions[0].pk

    def test_overlapping_periods_are_rejected_by_the_database(self):
        self._other_txn()
        for starts, expires in (
            ("2026-12-01", "2027-02-01"),  # inside the first period
            ("2027-05-01", "2027-06-01"),  # straddles the seam
        ):
            with self.subTest(starts=starts), self.assertRaises(IntegrityError):
                self.insert_sub(starts, expires, txn_id=self._free_txn())

    def _free_txn(self):
        return self._new_txn()

    def _new_txn(self):
        # a payment transaction not yet attached to a subscription (posted directly)
        from django.db import transaction as tx

        from finance import services as fs
        from finance.services import Item

        with tx.atomic():
            return (
                fs.post_items(
                    member=self.member,
                    items=[
                        Item(
                            fs.K.SUBSCRIPTION_PAYMENT,
                            fs.FLOW.IN,
                            100,
                            TODAY,
                            [Entry("cash", 100)],
                            label="x",
                        )
                    ],
                    actor=self.owner,
                    today=TODAY,
                )
                .transactions[0]
                .pk
            )

    def test_adjacent_periods_and_cancelled_periods_do_not_conflict(self):
        self.insert_sub("2027-05-11", "2027-11-11", txn_id=self._new_txn())
        self.insert_sub("2026-12-01", "2027-02-01", txn_id=self._new_txn(), cancelled=True)

    def test_expiry_must_follow_start_and_payment_is_unique_per_subscription(self):
        with self.assertRaises(IntegrityError):
            self.insert_sub("2028-01-01", "2028-01-01", txn_id=self._new_txn())
        with self.assertRaises(IntegrityError):
            self.insert_sub("2030-01-01", "2030-07-01")  # reuses the payment of the first period

    def test_dates_and_snapshots_cannot_be_edited(self):
        for column, value in (
            ("starts_on", "2026-11-12"),
            ("expires_on", "2027-06-01"),
            ("price_paise_snapshot", "1"),
            ("plan_name_snapshot", "'x'"),
            ("member_id", str(self.member.pk + 999)),
        ):
            with self.subTest(column), self.assertRaises(DatabaseError):
                execute(f"UPDATE memberships_membersubscription SET {column} = {value}")

    def test_subscriptions_are_never_deleted(self):
        with self.assertRaisesMessage(DatabaseError, "never deleted"):
            execute("DELETE FROM memberships_membersubscription")

    def test_cancellation_is_one_way_needs_a_reason_and_has_no_financial_effect(self):
        txns = FinancialTransaction.objects.count()
        with self.assertRaises(RuleViolation) as ctx:
            ms.cancel_subscription(actor=self.owner, subscription=self.sub, reason=" ")
        self.assertEqual(ctx.exception.code, "reason_required")
        ms.cancel_subscription(actor=self.owner, subscription=self.sub, reason="moved away")
        self.sub.refresh_from_db()
        self.assertIsNotNone(self.sub.cancelled_at)
        self.assertEqual(FinancialTransaction.objects.count(), txns)  # no refund, no credit
        with self.assertRaises(RuleViolation) as ctx:
            ms.cancel_subscription(actor=self.owner, subscription=self.sub, reason="again")
        self.assertEqual(ctx.exception.code, "already_cancelled")
        with self.assertRaisesMessage(DatabaseError, "cancelled subscription cannot change"):
            execute("UPDATE memberships_membersubscription SET cancel_reason = 'edited'")
        self.assertIsNone(ms.current_subscription(self.member, TODAY))

    def test_cancelling_frees_the_period_for_a_new_subscription(self):
        ms.cancel_subscription(actor=self.owner, subscription=self.sub, reason="wrong plan")
        sub = renew(
            self.owner, self.member, plan="12m", today=TODAY, activation_date=TODAY
        ).subscription
        self.assertEqual(sub.starts_on, TODAY)

    def test_cancellation_check_constraint(self):
        with self.assertRaises(IntegrityError):
            execute("UPDATE memberships_membersubscription SET cancelled_at = now()")


class TokenLedgerConstraintTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member = register(self.owner).member
        self.cycle = tokens.latest_cycle(self.member)
        self.txn = FinancialTransaction.objects.filter(kind="token_collection").get()

    def insert_entry(self, entry_type, delta, **kw):
        values = dict(
            cycle_id=self.cycle.pk,
            entry_type=entry_type,
            delta_paise=delta,
            forfeiture_trigger="",
            owner_confirmed=False,
            transaction_id=self.txn.pk,
            reverses_id=None,
            effective_date="2026-11-11",
            reason="",
        )
        values.update(kw)
        cols = ", ".join(values)
        execute(
            f"INSERT INTO finance_tokenledgerentry ({cols}, processed_at) "
            f"VALUES ({', '.join(['%s'] * len(values))}, now())",
            list(values.values()),
        )

    def test_a_cycle_has_at_most_one_collection_refund_and_forfeiture(self):
        with self.assertRaises(IntegrityError):
            self.insert_entry("collection", 50000)
        self.insert_entry(
            "forfeiture", -50000, forfeiture_trigger="lost", owner_confirmed=True, reason="lost"
        )
        with self.assertRaises(IntegrityError):
            self.insert_entry(
                "forfeiture", -1, forfeiture_trigger="lost", owner_confirmed=True, reason="x"
            )

    def test_signs_and_types_are_enforced(self):
        bad = [
            ("collection", -1, {}),
            ("refund", 5, {}),
            ("forfeiture", 0, dict(forfeiture_trigger="lost", owner_confirmed=True, reason="x")),
            ("carry_forward", 7, {}),
            ("mystery", 0, {}),
            ("reversal", -5, {}),  # reversal must link an original
            ("refund", -50000, dict(forfeiture_trigger="lost")),  # trigger only for forfeitures
            (
                "forfeiture",
                -50000,
                dict(forfeiture_trigger="bogus", owner_confirmed=True, reason="x"),
            ),
        ]
        for entry_type, delta, extra in bad:
            with (
                self.subTest(entry_type=entry_type, delta=delta),
                self.assertRaises(IntegrityError),
            ):
                self.insert_entry(entry_type, delta, **extra)

    def test_only_the_automatic_overdue_forfeiture_may_skip_owner_confirmation(self):
        with self.assertRaises(IntegrityError):
            self.insert_entry(
                "forfeiture", -50000, forfeiture_trigger="lost", owner_confirmed=False
            )
        with self.assertRaises(IntegrityError):  # confirmed forfeitures need a reason
            self.insert_entry(
                "forfeiture", -50000, forfeiture_trigger="damaged", owner_confirmed=True
            )
        self.insert_entry(
            "forfeiture", -50000, forfeiture_trigger="overdue_auto", owner_confirmed=False
        )

    def test_balance_can_never_leave_zero_to_amount(self):
        with self.assertRaisesMessage(DatabaseError, "balance"):
            with transaction.atomic():
                self.insert_entry(
                    "forfeiture",
                    -50000,
                    forfeiture_trigger="lost",
                    owner_confirmed=True,
                    reason="a",
                )
                self.insert_entry("refund", -50000)
                with connection.cursor() as cur:
                    cur.execute("SET CONSTRAINTS ALL IMMEDIATE")

    def test_token_tables_are_append_only(self):
        for table in ("finance_tokenaccount", "finance_tokencycle", "finance_tokenledgerentry"):
            with self.subTest(table), self.assertRaisesMessage(DatabaseError, "append-only"):
                execute(f"UPDATE {table} SET id = id")
            with (
                self.subTest(table + " delete"),
                self.assertRaisesMessage(DatabaseError, "append-only"),
            ):
                execute(f"DELETE FROM {table}")

    def test_cycle_numbers_are_unique_per_member_and_amounts_positive(self):
        with self.assertRaises(IntegrityError):
            execute(
                "INSERT INTO finance_tokencycle "
                "(account_id, cycle_no, amount_snapshot_paise, started_on) "
                "VALUES (%s, 1, 50000, '2026-11-11')",
                [self.cycle.account_id],
            )
        with self.assertRaises(IntegrityError):
            execute(
                "INSERT INTO finance_tokencycle "
                "(account_id, cycle_no, amount_snapshot_paise, started_on) "
                "VALUES (%s, 2, 0, '2026-11-11')",
                [self.cycle.account_id],
            )

    def test_forfeiture_helper_produces_a_consistent_ledger(self):
        forfeit(self.owner, self.member)
        self.assertEqual(tokens.balance_paise(self.cycle), 0)
        self.assertEqual(TokenLedgerEntry.objects.filter(entry_type="forfeiture").count(), 1)
