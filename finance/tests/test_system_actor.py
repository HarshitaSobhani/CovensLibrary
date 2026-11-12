"""D-15: the system actor (recorded_by_system) is valid only for the automatic day-8 forfeiture."""

import uuid
from datetime import date

from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TestCase

from accounts.models import User
from audit.models import AuditEvent
from core.errors import RuleViolation
from finance import corrections as fc
from finance import services as fs
from finance import tokens
from finance.models import SYSTEM_OVERDUE_FORFEITURE
from finance.services import Entry, Item
from memberships.tests.helpers import make_owner, register

TODAY = date(2026, 12, 25)


class SystemActorTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member = register(self.owner).member

    def auto(self, **kw):
        return fc.forfeit_token(
            member=self.member,
            trigger="overdue_auto",
            effective_date=date(2026, 12, 18),
            today=TODAY,
            **kw,
        )

    def test_the_automatic_forfeiture_is_attributed_to_the_system_not_a_user(self):
        txn = self.auto().transactions[0]
        self.assertIsNone(txn.recorded_by_id)
        self.assertEqual(txn.recorded_by_system, SYSTEM_OVERDUE_FORFEITURE)
        self.assertEqual(txn.effective_date, date(2026, 12, 18))
        event = AuditEvent.objects.get(action="token.forfeited")
        self.assertIsNone(event.actor_id)
        self.assertEqual(event.summary["system"], SYSTEM_OVERDUE_FORFEITURE)  # explicit in audit
        self.assertEqual(tokens.token_state(self.member)[1], tokens.FORFEITED)

    def test_no_user_row_is_created_and_the_single_owner_rule_is_untouched(self):
        self.auto()
        self.assertEqual(User.objects.count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user("second", password="Correct-Horse-9!")

    def test_owner_actions_keep_the_authenticated_owner(self):
        txn = fc.forfeit_token(
            actor=self.owner,
            member=self.member,
            trigger="lost",
            owner_confirmed=True,
            reason="not returned",
            today=TODAY,
        ).transactions[0]
        self.assertEqual((txn.recorded_by_id, txn.recorded_by_system), (self.owner.pk, ""))
        self.assertNotIn("system", AuditEvent.objects.get(action="token.forfeited").summary)

    def test_the_system_actor_cannot_be_used_for_confirmed_forfeitures_or_any_other_record(self):
        for kwargs, code in (
            (dict(trigger="lost", owner_confirmed=True, reason="r"), "actor_required"),
            (dict(trigger="damaged", owner_confirmed=True, reason="r"), "actor_required"),
        ):
            with self.assertRaises(RuleViolation) as ctx:
                fc.forfeit_token(member=self.member, today=TODAY, **kwargs)
            self.assertEqual(ctx.exception.code, code)
        with self.assertRaises(RuleViolation) as ctx:  # the automatic one refuses an owner actor
            fc.forfeit_token(
                actor=self.owner, member=self.member, trigger="overdue_auto", today=TODAY
            )
        self.assertEqual(ctx.exception.code, "auto_forfeiture_is_system")
        for kind, flow, entries in (
            (fs.K.ADJUSTMENT, fs.FLOW.IN, [Entry("cash", 100)]),
            (fs.K.TOKEN_COLLECTION, fs.FLOW.IN, [Entry("cash", 100)]),
        ):
            with self.assertRaises(RuleViolation) as ctx, transaction.atomic():
                fs.post_items(
                    member=self.member,
                    actor=None,
                    system=SYSTEM_OVERDUE_FORFEITURE,
                    today=TODAY,
                    items=[Item(kind, flow, 100, TODAY, entries, reason="r")],
                )
            self.assertEqual(ctx.exception.code, "system_actor_misuse")
        with self.assertRaises(RuleViolation) as ctx, transaction.atomic():  # no actor, no system
            fs.post_items(
                member=self.member,
                actor=None,
                today=TODAY,
                items=[Item(fs.K.TOKEN_FORFEITURE, fs.FLOW.NONE, 100, TODAY)],
            )
        self.assertEqual(ctx.exception.code, "actor_required")

    def test_reversing_an_automatic_forfeiture_is_an_owner_action(self):
        original = self.auto().transactions[0]
        reversal = fc.reverse_transaction(
            actor=self.owner, transaction_id=original.pk, reason="forfeited in error", today=TODAY
        ).transactions[0]
        self.assertEqual(
            (reversal.recorded_by_id, reversal.recorded_by_system), (self.owner.pk, "")
        )
        self.assertEqual(tokens.token_state(self.member)[1], tokens.HELD)


class SystemActorDatabaseTests(TestCase):
    """The same rules hold for raw SQL (CHECKs and trigger finance_ledger_attribution)."""

    def setUp(self):
        self.owner = make_owner()
        self.member = register(self.owner).member

    def insert_txn(
        self, kind="token_forfeiture", flow="none", by="NULL", system="'overdue_forfeiture_job'"
    ):
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO finance_financialtransaction (internal_txn_id, member_id, kind, "
                "cash_flow, amount_paise, effective_date, processed_at, receipt_sequence, "
                "receipt_year, receipt_no, reason, backdated, backdate_reason, "
                "backdate_financial_confirmed, recorded_by_id, recorded_by_system) "
                f"VALUES (%s, %s, %s, %s, 100, '2026-12-18', now(), '', NULL, '', '', false, '', "
                f"false, {by}, {system}) RETURNING id",
                [str(uuid.uuid4()), self.member.pk, kind, flow],
            )
            return cur.fetchone()[0]

    def test_exactly_one_attribution(self):
        for by, system in (("NULL", "''"), (str(self.owner.pk), "'overdue_forfeiture_job'")):
            with (
                self.subTest(by=by, system=system),
                self.assertRaises(IntegrityError),
                transaction.atomic(),
            ):
                self.insert_txn(by=by, system=system)

    def test_a_system_attribution_is_valid_only_on_a_forfeiture_with_the_known_name(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.insert_txn(kind="adjustment", flow="in", by="NULL")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.insert_txn(system="'some_other_job'")
        with transaction.atomic():
            self.assertTrue(self.insert_txn())  # the one valid combination

    def _ledger(self, txn_id, trigger, confirmed):
        cycle = tokens.latest_cycle(self.member)
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO finance_tokenledgerentry (cycle_id, entry_type, delta_paise, "
                "forfeiture_trigger, owner_confirmed, transaction_id, effective_date, "
                "processed_at, reason) VALUES (%s, 'forfeiture', -50000, %s, %s, %s, "
                "'2026-12-18', now(), %s)",
                [cycle.pk, trigger, confirmed, txn_id, "r" if confirmed else ""],
            )

    def test_a_system_transaction_may_only_back_an_overdue_auto_entry(self):
        with self.assertRaisesMessage(DatabaseError, "only for an automatic overdue forfeiture"):
            with transaction.atomic():
                self._ledger(self.insert_txn(), "lost", True)

    def test_an_overdue_auto_entry_needs_the_system_transaction(self):
        with self.assertRaisesMessage(DatabaseError, "must carry the system attribution"):
            with transaction.atomic():
                self._ledger(
                    self.insert_txn(by=str(self.owner.pk), system="''"), "overdue_auto", False
                )

    def test_the_valid_combination_is_accepted(self):
        with transaction.atomic():
            self._ledger(self.insert_txn(), "overdue_auto", False)
            transaction.set_rollback(True)
