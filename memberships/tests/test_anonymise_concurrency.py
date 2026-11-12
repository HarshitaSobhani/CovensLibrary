"""H5: anonymisation locks the member row like every payment, token and subscription operation."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase

from audit.models import AuditEvent
from core.errors import RuleViolation
from finance import corrections as fc
from finance import tokens
from finance.models import FinancialTransaction
from finance.services import Entry
from members import services as members
from members.models import Member
from memberships import services as ms
from memberships.models import MemberSubscription

from .helpers import TODAY, ensure_plans, forfeit, make_owner, register


def parallel(fns):
    def wrapped(fn):
        try:
            return fn()
        except Exception as exc:
            return exc
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        return list(pool.map(wrapped, fns))


class AnonymiseConcurrencyTests(TransactionTestCase):
    def setUp(self):
        ensure_plans()
        self.owner = make_owner()
        self.member = register(self.owner).member

    def tearDown(self):
        close_old_connections()

    def anonymise(self):
        return members.anonymise_member(
            Member.objects.get(pk=self.member.pk), actor=self.owner, reason="request", confirm=True
        )

    def test_anonymise_waits_for_an_operation_that_holds_the_member_lock(self):
        holding, release, finished = threading.Event(), threading.Event(), threading.Event()
        result = {}

        def holder():
            try:
                with transaction.atomic():
                    Member.objects.select_for_update().get(pk=self.member.pk)
                    holding.set()
                    release.wait(10)
            finally:
                connection.close()

        def worker():
            try:
                self.anonymise()
                result["done"] = True
            except Exception as exc:
                result["error"] = exc
            finally:
                finished.set()
                connection.close()

        t1 = threading.Thread(target=holder)
        t1.start()
        holding.wait(10)
        t2 = threading.Thread(target=worker)
        t2.start()
        time.sleep(0.6)
        self.assertFalse(finished.is_set(), "anonymise must wait for the member lock")
        release.set()
        t1.join(10)
        t2.join(10)
        self.assertTrue(finished.is_set())
        self.assertEqual(result, {"done": True})

    def test_two_simultaneous_anonymisations_succeed_exactly_once(self):
        results = parallel([self.anonymise] * 6)
        self.assertEqual(len([r for r in results if not isinstance(r, Exception)]), 1)
        self.assertEqual(
            {r.code for r in results if isinstance(r, RuleViolation)}, {"member_anonymised"}
        )
        self.assertEqual(AuditEvent.objects.filter(action="member.anonymise").count(), 1)

    def test_renewal_racing_anonymisation_is_all_or_nothing(self):
        def renew():
            return ms.renew_subscription(
                actor=self.owner,
                member=Member.objects.get(pk=self.member.pk),
                plan_code="6m",
                entries=[Entry("cash", 80000)],
                today=date(2027, 1, 1),
            )

        results = parallel([self.anonymise] + [renew] * 5)
        errors = [
            r for r in results if isinstance(r, Exception) and not isinstance(r, RuleViolation)
        ]
        self.assertEqual(errors, [])
        member = Member.objects.get(pk=self.member.pk)
        self.assertIsNotNone(member.anonymised_at)
        subs = MemberSubscription.objects.filter(member=member)
        self.assertLessEqual(subs.count(), 2)  # at most one renewal ever fits
        for sub in subs.exclude(starts_on=date(2026, 11, 11)):
            # a renewal that won the race committed BEFORE the anonymisation took the lock
            self.assertLess(sub.created_at, member.anonymised_at)
        codes = {r.code for r in results if isinstance(r, RuleViolation)}
        self.assertLessEqual(codes, {"anonymised", "scheduled_exists", "member_anonymised"})

    def test_fresh_token_racing_anonymisation_never_follows_it(self):
        forfeit(self.owner, self.member)

        def pay():
            return ms.pay_fresh_token(
                actor=self.owner,
                member=Member.objects.get(pk=self.member.pk),
                entries=[Entry("cash", 50000)],
                today=TODAY,
            )

        results = parallel([self.anonymise, pay, pay, pay])
        self.assertEqual(
            [r for r in results if isinstance(r, Exception) and not isinstance(r, RuleViolation)],
            [],
        )
        member = Member.objects.get(pk=self.member.pk)
        cycles = member.token_account.cycles.count()
        self.assertIn(cycles, (1, 2))
        if cycles == 2:
            created = (
                FinancialTransaction.objects.filter(member=member, kind="token_collection")
                .order_by("-id")
                .first()
            )
            self.assertLess(created.processed_at, member.anonymised_at)

    def test_refund_racing_anonymisation_pays_out_exactly_once_and_keeps_state_consistent(self):
        def refund():
            return fc.refund_token(
                actor=self.owner,
                member=Member.objects.get(pk=self.member.pk),
                entries=[Entry("cash", 50000)],
                today=TODAY,
            )

        results = parallel([self.anonymise, refund, refund, refund])
        self.assertEqual(
            len(
                [r for r in results if not isinstance(r, Exception) and hasattr(r, "transactions")]
            ),
            1,
        )
        member = Member.objects.get(pk=self.member.pk)
        self.assertIsNotNone(member.anonymised_at)
        self.assertEqual(tokens.token_state(member)[1], tokens.REFUNDED)
        self.assertEqual(FinancialTransaction.objects.filter(kind="token_refund").count(), 1)
        self.assertIn("anonymised", [c for c, _ in ms.borrowing_blockers(member, TODAY)])

    def test_anonymise_works_on_the_fresh_row_not_a_stale_object(self):
        stale = Member.objects.get(pk=self.member.pk)
        Member.objects.filter(pk=self.member.pk).update(notes="changed meanwhile")
        members.anonymise_member(stale, actor=self.owner, reason="request", confirm=True)
        row = Member.objects.get(pk=self.member.pk)
        self.assertEqual((row.notes, row.full_name), ("", "Anonymised member"))
        self.assertEqual(stale.notes, "")  # the caller's object was refreshed too
