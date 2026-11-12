"""D-26: notification data is erased INSIDE the anonymisation transaction, by state."""

import smtplib
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.db import DatabaseError, connection, transaction
from django.test import TestCase
from django.utils import timezone

from audit.models import AuditEvent
from circulation.tests.helpers import make_owner, member_at
from communications import services as cs
from communications.models import Notification, NotificationAttempt
from members import services as members
from members.models import Member

from .helpers import give_email, ist

N = Notification
S = Notification.Status
NAME = "Zainab Qureshi"
ADDRESS = "zainab.q@example.com"


def fail_smtp(text="Recipient address rejected: zainab.q@example.com"):
    return mock.patch.object(cs.EmailMessage, "send", side_effect=smtplib.SMTPException(text))


class RedactionTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member = give_email(member_at(self.owner, name=NAME, number="9811100077"), ADDRESS)
        cs.consume_events(ist(2026, 11, 11))
        self.t0 = ist(2026, 11, 11, 10)

    def anonymise(self):
        members.anonymise_member(self.member, actor=self.owner, reason="request", confirm=True)

    def notes(self, **kw):
        return N.objects.filter(member=self.member, **kw)

    def personal(self, note):
        return " ".join(
            [note.recipient_email, note.subject, note.body, note.last_error, note.skip_reason]
        )

    def test_prepared_notifications_are_skipped_and_erased_at_once(self):
        self.assertTrue(self.notes(status=S.PREPARED).exists())
        self.anonymise()
        for note in self.notes(kind="payment_receipt"):
            self.assertEqual((note.status, note.skip_code), (S.SKIPPED, "anonymised"))
            self.assertEqual((note.recipient_email, note.subject, note.body), ("", "", ""))
        self.assertFalse(self.notes(status=S.PREPARED).exists())
        cs.send_due(self.t0)
        self.assertEqual(len(mail.outbox), 0)  # nothing is sent after anonymisation

    def test_retry_pending_notifications_are_erased_without_waiting_for_the_retry(self):
        with fail_smtp():
            cs.send_due(self.t0)
        pending = self.notes(status=S.SMTP_FAILED)
        self.assertTrue(pending.exists())
        self.assertIn(ADDRESS, pending.first().recipient_email)
        self.anonymise()  # NO cycle runs afterwards: the erasure must already be done
        self.assertFalse(self.notes(status=S.SMTP_FAILED).exists())
        for note in self.notes(kind="payment_receipt"):
            self.assertEqual(
                (note.status, note.recipient_email, note.subject, note.body),
                (S.SKIPPED, "", "", ""),
            )
            self.assertIsNone(note.next_attempt_at)
            self.assertEqual(note.last_error, "SMTPException (details removed)")
            self.assertNotIn(NAME, self.personal(note))
        with fail_smtp():
            self.assertEqual(cs.send_due(self.t0 + timedelta(hours=3)), {})

    def test_failed_notifications_become_skipped_and_erased(self):
        t = self.t0
        with fail_smtp():
            for delay in (0, 5, 30, 120):
                t = t + timedelta(minutes=delay)
                cs.send_due(t)
        failed = self.notes(status=S.FAILED)
        self.assertTrue(failed.exists())
        self.anonymise()
        self.assertFalse(self.notes(status=S.FAILED).exists())
        note = self.notes(kind="payment_receipt").get()
        self.assertEqual(
            (note.status, note.skip_code, note.recipient_email), (S.SKIPPED, "anonymised", "")
        )
        self.assertEqual((note.subject, note.body), ("", ""))
        self.assertEqual(note.attempt_count, 4)  # the history of attempts is kept

    def test_accepted_notifications_keep_only_the_address_and_time(self):
        cs.send_due(self.t0)
        accepted = self.notes(status=S.SMTP_ACCEPTED)
        self.assertTrue(accepted.exists())
        self.anonymise()
        for note in self.notes(status=S.SMTP_ACCEPTED):
            self.assertEqual(note.recipient_email, ADDRESS)
            self.assertEqual(note.accepted_at, self.t0)
            self.assertEqual((note.subject, note.body), ("", ""))
            self.assertNotIn(NAME, self.personal(note))

    def backdate_anonymisation(self, days):
        Member.objects.filter(pk=self.member.pk).update(
            anonymised_at=timezone.now() - timedelta(days=days)
        )

    def test_the_retained_address_is_erased_after_90_days_and_not_before(self):
        cs.send_due(self.t0)
        self.anonymise()
        self.backdate_anonymisation(89)
        self.assertEqual(cs.redact_expired_addresses(timezone.now()), 0)
        self.backdate_anonymisation(91)
        self.assertEqual(cs.redact_expired_addresses(timezone.now()), 2)
        for note in self.notes(status=S.SMTP_ACCEPTED):
            self.assertEqual((note.recipient_email, note.accepted_at), ("", self.t0))
            self.assertEqual(note.status, S.SMTP_ACCEPTED)  # status and time stay for reports
        self.assertEqual(cs.redact_expired_addresses(timezone.now()), 0)

    def test_run_cycle_performs_the_scheduled_erasure(self):
        cs.send_due(self.t0)
        self.anonymise()
        self.backdate_anonymisation(91)
        self.assertEqual(cs.run_cycle(timezone.now())["redacted"], 2)

    def test_attempts_and_errors_never_hold_the_address(self):
        with fail_smtp():
            cs.send_due(self.t0)
        for attempt in NotificationAttempt.objects.all():
            self.assertNotIn(ADDRESS, attempt.error)
            self.assertIn("<recipient>", attempt.error)
        self.assertFalse(hasattr(NotificationAttempt, "recipient_email"))
        for note in self.notes(status=S.SMTP_FAILED):
            self.assertNotIn(ADDRESS, note.last_error)

    def test_the_redaction_is_audited_without_personal_data(self):
        self.anonymise()
        event = AuditEvent.objects.get(action="notifications.redacted")
        self.assertGreater(event.summary["records"], 0)
        self.assertNotIn(NAME, str(event.summary))
        self.assertNotIn(ADDRESS, str(event.summary))

    def test_a_failure_inside_the_hook_rolls_the_whole_anonymisation_back(self):
        with mock.patch.object(cs, "redact_member_notifications", side_effect=RuntimeError("boom")):
            hooks = members.ANONYMISATION_HOOKS
            original = list(hooks)
            hooks[:] = [cs.redact_member_notifications]
            try:
                with self.assertRaises(RuntimeError):
                    self.anonymise()
            finally:
                hooks[:] = original
        member = Member.objects.get(pk=self.member.pk)
        self.assertIsNone(member.anonymised_at)
        self.assertEqual(member.full_name, NAME)
        self.assertTrue(self.notes(status=S.PREPARED).exists())
        self.assertFalse(AuditEvent.objects.filter(action="member.anonymise").exists())

    def test_the_hook_is_registered(self):
        self.assertIn(cs.redact_member_notifications, members.ANONYMISATION_HOOKS)

    def test_a_cycle_never_sends_a_message_in_flight_after_the_claim_is_cleared(self):
        token, ids = cs.claim_due(self.t0)
        self.anonymise()
        for note_id in ids:
            self.assertIsNone(cs.attempt(note_id, token, self.t0))
        self.assertEqual(len(mail.outbox), 0)

    def test_other_members_are_untouched(self):
        other = give_email(
            member_at(self.owner, name="Other Person", number="9811100078"), "o@example.com"
        )
        cs.consume_events(ist(2026, 11, 11))
        before = N.objects.filter(member=other).count()
        self.anonymise()
        self.assertEqual(
            N.objects.filter(member=other, status=S.PREPARED).count(),
            before - N.objects.filter(member=other, status=S.SKIPPED).count(),
        )
        self.assertTrue(N.objects.filter(member=other, status=S.PREPARED).exists())
        self.assertTrue(
            all(
                n.recipient_email == "o@example.com"
                for n in N.objects.filter(member=other, status=S.PREPARED)
            )
        )


class RedactionTriggerTests(TestCase):
    def setUp(self):
        owner = make_owner()
        self.member = give_email(member_at(owner, name=NAME, number="9811100077"), ADDRESS)
        cs.run_cycle(ist(2026, 11, 11, 10))
        self.accepted = N.objects.filter(status=S.SMTP_ACCEPTED).first()

    def refused(self, sql, params=()):
        with self.assertRaises(DatabaseError):
            with transaction.atomic(), connection.cursor() as cur:
                cur.execute(sql, params)

    def test_a_finished_record_of_a_live_member_cannot_be_redacted_by_sql(self):
        self.refused(
            "UPDATE communications_notification SET body='' WHERE id=%s", [self.accepted.pk]
        )

    def test_after_anonymisation_only_erasure_is_allowed(self):
        members.anonymise_member(self.member, actor=make_owner_user(), reason="r", confirm=True)
        n = self.accepted
        self.refused("UPDATE communications_notification SET status='failed' WHERE id=%s", [n.pk])
        self.refused("UPDATE communications_notification SET accepted_at=now() WHERE id=%s", [n.pk])
        self.refused("UPDATE communications_notification SET body='hello' WHERE id=%s", [n.pk])
        self.refused(
            "UPDATE communications_notification SET recipient_email='' WHERE id=%s", [n.pk]
        )  # < 90 days
        self.refused("UPDATE communications_notification SET attempt_count=0 WHERE id=%s", [n.pk])
        self.refused("DELETE FROM communications_notification WHERE id=%s", [n.pk])
        Member.objects.filter(pk=self.member.pk).update(
            anonymised_at=Member.objects.get(pk=self.member.pk).anonymised_at - timedelta(days=91)
        )
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE communications_notification SET recipient_email='' WHERE id=%s", [n.pk]
            )


def make_owner_user():
    from django.contrib.auth import get_user_model

    return get_user_model().objects.get(username="owner")
