"""Phase 5 hardening: exact retention boundary, real concurrency on PostgreSQL, audit actor,
and the deployment safeguards (historic-record preflight, migration guard)."""

import importlib
import threading
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from audit.models import AuditEvent
from circulation.tests.helpers import make_owner, member_at
from communications import services as cs
from communications.models import Notification, NotificationAttempt
from members import services as members
from members.models import Member
from memberships.tests.helpers import ensure_plans

from .helpers import give_email, ist

S = Notification.Status
WAIT = 15  # seconds: only an upper bound for a failing run, never a pacing sleep


class RetentionBoundaryTests(TransactionTestCase):
    """Exact day-90 behaviour in the app AND in the database trigger (real PostgreSQL clock)."""

    def setUp(self):
        ensure_plans()
        self.owner = make_owner()
        self.member = give_email(member_at(self.owner), "zq@example.com")
        cs.run_cycle(ist(2026, 11, 11, 10))
        members.anonymise_member(self.member, actor=self.owner, reason="r", confirm=True)
        self.accepted = Notification.objects.filter(status=S.SMTP_ACCEPTED)

    def anonymised_ago(self, delta):
        now = timezone.now()
        Member.objects.filter(pk=self.member.pk).update(anonymised_at=now - delta)
        return now

    def addresses(self):
        return set(self.accepted.values_list("recipient_email", flat=True))

    def test_app_boundary_is_inclusive_at_exactly_90_days(self):
        now = self.anonymised_ago(timedelta(days=89, hours=23, minutes=59, seconds=59))
        self.assertEqual(cs.redact_expired_addresses(now), 0)
        self.assertEqual(self.addresses(), {"zq@example.com"})
        now = self.anonymised_ago(timedelta(days=90))
        self.assertEqual(cs.redact_expired_addresses(now), self.accepted.count())
        self.assertEqual(self.addresses(), {""})

    def test_day_91_is_erased_too(self):
        now = self.anonymised_ago(timedelta(days=91))
        self.assertGreater(cs.redact_expired_addresses(now), 0)
        self.assertEqual(self.addresses(), {""})

    def test_the_database_itself_refuses_an_early_erasure_and_allows_day_90(self):
        pk = self.accepted.first().pk
        self.anonymised_ago(timedelta(days=89, hours=23, minutes=59))
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
            cur.execute(
                "UPDATE communications_notification SET recipient_email='' WHERE id=%s", [pk]
            )
        self.anonymised_ago(timedelta(days=90))
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE communications_notification SET recipient_email='' WHERE id=%s", [pk]
            )
        self.assertEqual(Notification.objects.get(pk=pk).recipient_email, "")

    def test_status_and_accepted_time_survive_the_address_erasure(self):
        times = {n.pk: n.accepted_at for n in self.accepted}
        cs.redact_expired_addresses(self.anonymised_ago(timedelta(days=120)))
        for n in Notification.objects.filter(pk__in=times):
            self.assertEqual((n.status, n.accepted_at), (S.SMTP_ACCEPTED, times[n.pk]))


class ConcurrencyBase(TransactionTestCase):
    def setUp(self):
        ensure_plans()
        self.owner = make_owner()
        self.member = give_email(
            member_at(self.owner, name="Zainab Qureshi", number="9811100077"), "zq@example.com"
        )
        cs.consume_events(ist(2026, 11, 11))

    def in_threads(self, targets):
        barrier = threading.Barrier(len(targets))
        results, errors = [None] * len(targets), []

        def run(i, target):
            try:
                barrier.wait(WAIT)
                results[i] = target()
            except Exception as exc:  # noqa: BLE001 - reported to the test below
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=run, args=(i, t)) for i, t in enumerate(targets)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(WAIT * 2)
            self.assertFalse(t.is_alive(), "a worker thread did not finish")
        self.assertEqual(errors, [])
        return results


class ConcurrentReminderTests(ConcurrencyBase):
    def test_five_workers_scheduling_the_same_reminder_create_exactly_one_record(self):
        now = ist(2027, 4, 26, 9, 30)  # the member's expiry-reminder day
        created = self.in_threads([lambda: cs.schedule_reminders(now)] * 5)
        self.assertEqual(sum(created), 1)
        rows = Notification.objects.filter(source="schedule")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().scheduled_for.isoformat(), "2027-04-26")

    def test_repeated_full_cycles_in_parallel_still_send_each_email_once(self):
        now = ist(2027, 4, 26, 9, 30)
        self.in_threads([lambda: cs.run_cycle(now)] * 4)
        self.assertEqual(Notification.objects.filter(source="schedule").count(), 1)
        keys = list(Notification.objects.values_list("dedupe_key", flat=True))
        self.assertEqual(len(keys), len(set(keys)))
        accepted = Notification.objects.filter(status=S.SMTP_ACCEPTED)
        self.assertEqual(len(mail.outbox), accepted.count())
        for n in accepted:
            self.assertEqual(n.attempts.count(), 1)


class InFlightAnonymisationTests(ConcurrencyBase):
    """A send is blocked mid-flight (synchronised with Events, no sleeps) while the member is
    anonymised in the main thread."""

    def run_scenario(self):
        t0 = ist(2026, 11, 11, 10)
        token, ids = cs.claim_due(t0)
        entered, release = threading.Event(), threading.Event()
        calls, out = [], {}

        def blocked_send(message, fail_silently=False):
            calls.append(list(message.to))
            entered.set()
            self.assertTrue(release.wait(WAIT))
            return 1

        def worker():
            try:
                with mock.patch.object(cs.EmailMessage, "send", blocked_send):
                    out["result"] = cs.attempt(ids[0], token, t0)
            finally:
                connection.close()

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(entered.wait(WAIT), "the send never started")
        members.anonymise_member(self.member, actor=self.owner, reason="r", confirm=True)
        release.set()
        thread.join(WAIT)
        self.assertFalse(thread.is_alive())
        return t0, ids[0], out["result"], calls

    def test_the_in_flight_result_is_discarded_and_nothing_personal_returns(self):
        t0, note_id, result, calls = self.run_scenario()
        note = Notification.objects.get(pk=note_id)
        self.assertEqual(len(calls), 1)  # the message already left; it cannot be recalled
        self.assertIsNone(result)  # ...but its outcome is not recorded
        self.assertEqual((note.status, note.skip_code), (S.SKIPPED, "anonymised"))
        self.assertEqual((note.recipient_email, note.subject, note.body), ("", "", ""))
        self.assertEqual(
            (note.attempt_count, note.accepted_at, note.next_attempt_at), (0, None, None)
        )
        self.assertEqual(NotificationAttempt.objects.filter(notification=note).count(), 0)

    def test_no_automatic_resend_after_the_anonymisation(self):
        t0, note_id, result, calls = self.run_scenario()
        with mock.patch.object(cs.EmailMessage, "send", side_effect=AssertionError("resent")):
            for later in (
                t0 + timedelta(minutes=11),
                t0 + timedelta(hours=3),
                t0 + timedelta(days=2),
            ):
                cs.run_cycle(later)
        self.assertEqual(Notification.objects.get(pk=note_id).status, S.SKIPPED)
        self.assertFalse(
            Notification.objects.filter(member=self.member, subject__gt="").exists()
            or Notification.objects.filter(member=self.member, body__gt="").exists()
        )

    def test_the_audit_trail_shows_the_redaction_with_the_owner_as_actor(self):
        self.run_scenario()
        event = AuditEvent.objects.get(action="notifications.redacted")
        self.assertEqual(event.actor, self.owner)

    def test_a_result_that_commits_first_is_then_stripped_by_the_anonymisation(self):
        cs.send_due(ist(2026, 11, 11, 10))  # SMTP accepted first
        members.anonymise_member(self.member, actor=self.owner, reason="r", confirm=True)
        for n in Notification.objects.filter(status=S.SMTP_ACCEPTED):
            self.assertEqual((n.subject, n.body, n.recipient_email), ("", "", "zq@example.com"))


class AuditActorAndRollbackTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member = give_email(member_at(self.owner), "a@example.com")
        cs.consume_events(ist(2026, 11, 11))

    def test_the_redaction_audit_names_the_owner_and_has_no_personal_data(self):
        members.anonymise_member(
            self.member, actor=self.owner, reason="private reason", confirm=True
        )
        event = AuditEvent.objects.get(action="notifications.redacted")
        self.assertEqual(event.actor, self.owner)
        self.assertEqual(event.entity_id, self.member.membership_no)
        self.assertGreater(event.summary["records"], 0)
        self.assertNotIn("a@example.com", str(event.summary))
        self.assertNotIn("private reason", str(event.summary))

    def test_a_failure_after_the_redaction_rolls_back_notifications_audit_and_member(self):
        original = members.record

        def failing(*args, **kwargs):
            if args and args[0] == "member.anonymise" or kwargs.get("action") == "member.anonymise":
                raise RuntimeError("audit failed")
            return original(*args, **kwargs)

        with mock.patch.object(members, "record", failing):
            with self.assertRaises(RuntimeError):
                members.anonymise_member(self.member, actor=self.owner, reason="r", confirm=True)
        member = Member.objects.get(pk=self.member.pk)
        self.assertIsNone(member.anonymised_at)
        self.assertTrue(Notification.objects.filter(member=member, status=S.PREPARED).exists())
        self.assertFalse(AuditEvent.objects.filter(action="notifications.redacted").exists())
        self.assertTrue(Notification.objects.filter(member=member).exclude(body="").exists())

    def test_the_hook_receives_the_actor(self):
        seen = []
        hooks = members.ANONYMISATION_HOOKS
        original = list(hooks)
        hooks[:] = [lambda member, actor=None: seen.append(actor)]
        try:
            members.anonymise_member(self.member, actor=self.owner, reason="r", confirm=True)
        finally:
            hooks[:] = original
        self.assertEqual(seen, [self.owner])


class PreflightTests(TestCase):
    """Historic records: an anonymisation that happened BEFORE the redaction hook existed."""

    def setUp(self):
        self.owner = make_owner()
        self.member = give_email(
            member_at(self.owner, name="Old Case", number="9811100055"), "old@example.com"
        )
        cs.run_cycle(ist(2026, 11, 11, 10))  # accepted emails with body and address
        cs.consume_events(ist(2026, 12, 1))
        Notification.objects.create(
            dedupe_key="legacy:open", source="schedule", kind="loan_issued", member=self.member,
            recipient_email="old@example.com", subject="s", body="b", status="prepared",
        )  # fmt: skip
        hooks = members.ANONYMISATION_HOOKS
        original = list(hooks)
        hooks[:] = []  # simulate the old behaviour: nothing redacted
        try:
            members.anonymise_member(self.member, actor=self.owner, reason="r", confirm=True)
        finally:
            hooks[:] = original

    def run_command(self, *args):
        out = StringIO()
        call_command("preflight_notifications", *args, stdout=out)
        return out.getvalue()

    def test_the_preflight_finds_historic_personal_data_and_fails_without_apply(self):
        self.assertGreater(cs.find_unredacted().count(), 0)
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertGreater(cs.find_unredacted().count(), 0)  # a report changes nothing

    def test_apply_redacts_then_verifies_and_is_idempotent(self):
        out = self.run_command("--apply")
        self.assertIn("Verified: nothing remains", out)
        self.assertEqual(cs.find_unredacted().count(), 0)
        for n in Notification.objects.filter(member=self.member):
            self.assertEqual((n.subject, n.body), ("", ""))
        self.assertEqual(Notification.objects.get(dedupe_key="legacy:open").status, S.SKIPPED)
        self.assertIn("Nothing to redact", self.run_command())
        self.assertIn("Nothing to redact", self.run_command("--apply"))

    def test_a_clean_database_passes(self):
        cs.redact_all_anonymised()
        self.assertIn("Nothing to redact", self.run_command())

    def test_live_members_are_never_touched(self):
        other = give_email(
            member_at(self.owner, name="Live One", number="9811100056"), "live@example.com"
        )
        cs.consume_events(ist(2026, 11, 11))
        before = list(
            Notification.objects.filter(member=other).values_list("pk", "body", "recipient_email")
        )
        self.run_command("--apply")
        self.assertEqual(
            list(
                Notification.objects.filter(member=other).values_list(
                    "pk", "body", "recipient_email"
                )
            ),
            before,
        )


class MigrationSafeguardTests(TestCase):
    def guard(self):
        module = importlib.import_module("communications.migrations.0003_retention_and_backfill")
        from django.apps import apps

        return module.refuse_if_data_exists, apps, module.OVERRIDE

    def test_it_runs_on_an_empty_database_and_refuses_real_data(self):
        refuse, apps, override = self.guard()
        refuse(apps, None)  # nothing recorded: allowed
        owner = make_owner()
        give_email(member_at(owner))
        cs.consume_events(ist(2026, 11, 11))
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop(override, None)
            with self.assertRaises(RuntimeError) as ctx:
                refuse(apps, None)
        self.assertIn("DATABASE_SECURITY.md", str(ctx.exception))
        with mock.patch.dict("os.environ", {override: "1"}):
            refuse(apps, None)  # explicit, documented override

    def test_reminders_without_a_recorded_date_show_their_creation_date(self):
        owner = make_owner()
        member = member_at(owner)
        n = Notification.objects.create(
            dedupe_key="legacy:rem", source="schedule", kind="expiry_reminder", member=member,
            status="skipped", skip_code="no_email", created_at=ist(2027, 4, 26, 9, 5),
        )  # fmt: skip
        self.assertIsNone(n.scheduled_for)
        self.assertEqual(n.scheduled_date.isoformat(), "2027-04-26")
        event_note = Notification.objects.create(
            dedupe_key="e:1",
            source="event",
            kind="none",
            member=member,
            status="skipped",
            skip_code="not_notified",
        )
        self.assertIsNone(event_note.scheduled_date)
