"""Phase 5b: outbox consumption, scheduled reminders, SMTP sending and the retry schedule."""

import smtplib
import threading
from datetime import timedelta
from datetime import timezone as dt_timezone
from unittest import mock

from django.core import mail
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings

from circulation import forfeiture, services
from circulation.tests.helpers import D, make_owner, member_at, overdue_loan
from communications import services as cs
from communications.models import Notification, NotificationAttempt
from core.models import DomainEvent
from finance.services import Entry
from members import services as members
from memberships import services as ms

from .helpers import give_email, ist

N = Notification
S = Notification.Status


def fail_smtp():
    return mock.patch.object(cs.EmailMessage, "send", side_effect=smtplib.SMTPException("boom"))


class ConsumptionTests(TestCase):
    def setUp(self):
        self.owner = make_owner()

    def test_every_event_gets_exactly_one_record_and_a_second_run_adds_none(self):
        member = member_at(self.owner)
        give_email(member)
        events = DomainEvent.objects.count()
        self.assertEqual(cs.consume_events(ist(2026, 11, 11)), events)
        self.assertEqual(N.objects.count(), events)
        self.assertEqual(cs.consume_events(ist(2026, 11, 11)), 0)
        self.assertEqual(set(N.objects.values_list("dedupe_key", flat=True)),
                         set(DomainEvent.objects.values_list("dedupe_key", flat=True)))  # fmt: skip

    def test_registration_sends_activation_and_receipt_but_not_bookkeeping_events(self):
        give_email(member_at(self.owner))
        cs.consume_events(ist(2026, 11, 11))
        by_type = {
            e.event_type: N.objects.get(dedupe_key=e.dedupe_key) for e in DomainEvent.objects.all()
        }
        self.assertEqual(by_type["subscription.activated"].status, S.PREPARED)
        self.assertEqual(by_type["subscription.activated"].kind, "subscription_activation")
        self.assertEqual(by_type["payment.receipted"].kind, "payment_receipt")
        for bookkeeping in ("member.registered", "token.collected"):
            self.assertEqual(by_type[bookkeeping].status, S.SKIPPED)
            self.assertEqual(by_type[bookkeeping].skip_code, "not_notified")
        receipt = by_type["payment.receipted"]
        self.assertIn("SUB-2026-000001", receipt.subject)
        self.assertIn("TOK-2026-000001", receipt.body)
        self.assertIn("₹800.00", receipt.body)

    def test_loan_events_map_to_their_emails_with_title_and_dates(self):
        member, copy, loan = overdue_loan(self.owner)
        give_email(member)
        services.renew_loan(actor=self.owner, loan=loan, today=D(2026, 12, 20))
        services.return_loan(actor=self.owner, loan=loan, today=D(2026, 12, 22))
        cs.consume_events(ist(2026, 12, 22))
        got = {n.kind: n for n in N.objects.exclude(kind="none")}
        self.assertIn("B-9876543210", got["loan_issued"].subject)
        self.assertIn("31 December 2026", got["loan_issued"].body)
        self.assertIn("30 January 2027", got["loan_renewed"].body)
        self.assertIn("22 December 2026", got["loan_returned"].body)

    def test_no_email_address_is_skipped_with_the_reason(self):
        member_at(self.owner)  # no email
        cs.consume_events(ist(2026, 11, 11))
        note = N.objects.get(kind="payment_receipt")
        self.assertEqual(
            (note.status, note.skip_code, note.skip_reason),
            (S.SKIPPED, "no_email", "no email address"),
        )
        self.assertEqual((note.recipient_email, note.body), ("", ""))

    def test_an_anonymised_member_is_skipped_and_nothing_personal_is_kept(self):
        member = give_email(member_at(self.owner, name="Zainab Qureshi"))
        members.anonymise_member(member, actor=self.owner, reason="request", confirm=True)
        cs.consume_events(ist(2026, 12, 1))
        notes = N.objects.exclude(kind="none")
        self.assertTrue(notes.exists())
        for note in notes:
            self.assertEqual((note.status, note.skip_code), (S.SKIPPED, "anonymised"))
            self.assertNotIn("Zainab", note.subject + note.body + note.skip_reason)
            self.assertEqual(note.recipient_email, "")

    def test_the_recipient_is_resolved_from_the_member_at_processing_time(self):
        member = member_at(self.owner)
        give_email(member, "first@example.com")
        cs.consume_events(ist(2026, 11, 11))
        self.assertEqual(N.objects.get(kind="payment_receipt").recipient_email, "first@example.com")
        self.assertFalse(
            any("first@example.com" in str(e.payload) for e in DomainEvent.objects.all())
        )  # the immutable events never hold the address

    def test_an_interruption_mid_consumption_loses_nothing(self):
        give_email(member_at(self.owner))
        real = cs._create
        calls = []

        def flaky(**kw):
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError("power cut")
            return real(**kw)

        with mock.patch.object(cs, "_create", side_effect=flaky):
            with self.assertRaises(RuntimeError):
                cs.consume_events(ist(2026, 11, 11))
        done = N.objects.count()
        self.assertEqual(done, 2)
        self.assertGreater(cs.unconsumed_events().count(), 0)
        cs.consume_events(ist(2026, 11, 11))
        self.assertEqual(N.objects.count(), DomainEvent.objects.count())
        self.assertEqual(cs.unconsumed_events().count(), 0)

    def test_the_day_8_forfeiture_notice_is_sent_straight_after_processing(self):
        member, copy, loan = overdue_loan(self.owner)
        give_email(member)
        cs.run_cycle(ist(2026, 12, 31, 12))  # earlier mails go out
        mail.outbox.clear()
        forfeiture.process_overdue_forfeitures(today=D(2027, 3, 1))  # processed late
        result = cs.run_cycle(ist(2027, 3, 1, 12))
        self.assertEqual(result["sent"], {S.SMTP_ACCEPTED: 1})
        message = mail.outbox[0]
        self.assertIn("token was forfeited", message.subject)
        self.assertIn("8 January 2027", message.body)  # the original effective date
        self.assertEqual(message.to, ["asha@example.com"])
        self.assertEqual(DomainEvent.objects.filter(event_type="token.forfeited").count(), 1)

    def test_an_event_whose_member_cannot_be_found_stays_unconsumed(self):
        from core import outbox

        with transaction.atomic():
            outbox.emit(outbox.LOAN_ISSUED, dedupe_key="orphan:1", member_id=987654, payload={})
        self.assertIsNone(cs.consume_event(DomainEvent.objects.get(dedupe_key="orphan:1")))
        self.assertEqual(cs.unconsumed_events().count(), DomainEvent.objects.count())


class SendingTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member = give_email(member_at(self.owner))
        cs.consume_events(ist(2026, 11, 11))
        self.receipt = N.objects.get(kind="payment_receipt")

    def test_success_is_recorded_as_smtp_accepted_never_delivered(self):
        t0 = ist(2026, 11, 11, 10)
        result = cs.send_due(t0)
        self.assertEqual(result, {S.SMTP_ACCEPTED: 2})  # activation + receipt
        note = N.objects.get(pk=self.receipt.pk)
        self.assertEqual(
            (note.status, note.attempt_count, note.accepted_at), (S.SMTP_ACCEPTED, 1, t0)
        )
        self.assertEqual(note.recipient_email, "asha@example.com")
        self.assertEqual(note.get_status_display(), "SMTP accepted")
        self.assertNotIn("deliver", " ".join(s.label.lower() for s in S))
        self.assertEqual(len(mail.outbox), 2)
        attempt = note.attempts.get()
        self.assertEqual(
            (attempt.accepted, attempt.error),
            (True, ""),
        )

    def test_a_lapsed_lease_is_never_sent_by_the_stalled_worker(self):  # C-02
        # Worker A claims both notifications at t0, then stalls. Its lease lapses; worker B
        # claims and sends them. When A wakes up it must not send anything.
        t0 = ist(2026, 11, 11, 10)
        token_a, ids_a = cs.claim_due(t0, limit=10)
        late = t0 + cs.LEASE + timedelta(seconds=1)
        self.assertEqual(cs.send_due(late), {S.SMTP_ACCEPTED: 2})  # worker B
        self.assertEqual(len(mail.outbox), 2)
        for note_id in ids_a:  # worker A wakes up with stale claims
            self.assertIsNone(cs.attempt(note_id, token_a, late))
        self.assertEqual(len(mail.outbox), 2)  # still exactly one email per notification
        self.assertEqual(N.objects.filter(attempt_count=1).count(), 2)

    def test_a_running_worker_renews_its_lease_so_no_one_else_can_claim(self):  # C-02
        # Two notifications. The worker renews the lease of the row it still holds before each
        # send. A second worker that looks 11 minutes later must find nothing to claim, because
        # the lease was renewed at t0 + 9 minutes (it would be claimable without the renewal).
        t0 = ist(2026, 11, 11, 10)
        ticks = [t0]  # the claim happens at t0; everything after runs at t0 + 9 minutes
        clock = lambda: ticks.pop() if ticks else t0 + timedelta(minutes=9)  # noqa: E731
        seen = {}

        def during_first_send(*args, **kwargs):
            seen["second_worker"] = cs.claim_due(t0 + timedelta(minutes=11), limit=10)[1]
            return 1

        with (
            mock.patch.object(cs.timezone, "now", clock),
            mock.patch.object(
                cs.EmailMessage,
                "send",
                autospec=True,
                side_effect=lambda *a, **k: during_first_send(),
            ),
        ):
            result = cs.send_due()
        self.assertEqual(result, {S.SMTP_ACCEPTED: 2})
        self.assertEqual(seen["second_worker"], [])
        self.assertEqual(len(mail.outbox), 0)  # the mocked send records nothing; no duplicates

    def test_a_slow_smtp_server_within_the_lease_still_sends_each_email_once(self):  # C-02
        t0 = ist(2026, 11, 11, 10)
        self.assertEqual(cs.send_due(t0), {S.SMTP_ACCEPTED: 2})
        self.assertEqual(cs.send_due(t0 + timedelta(minutes=1)), {})
        self.assertEqual(len(mail.outbox), 2)

    def test_running_the_cycle_again_never_sends_a_second_email(self):
        cs.send_due(ist(2026, 11, 11, 10))
        cs.run_cycle(ist(2026, 11, 11, 10, 1))
        cs.run_cycle(ist(2026, 11, 11, 11))
        self.assertEqual(len(mail.outbox), 2)

    def test_the_approved_retry_schedule_then_failed_with_the_error_kept(self):
        t0 = ist(2026, 11, 11, 10)
        with fail_smtp():
            cs.send_due(t0)
            note = N.objects.get(pk=self.receipt.pk)
            self.assertEqual((note.status, note.attempt_count), (S.SMTP_FAILED, 1))
            self.assertEqual(note.next_attempt_at, t0 + timedelta(minutes=5))  # retry 1
            self.assertEqual(cs.send_due(t0 + timedelta(minutes=4, seconds=59)), {})  # too early
            t1 = t0 + timedelta(minutes=5, seconds=20)  # retry 1 happens a little late
            cs.send_due(t1)
            note.refresh_from_db()
            self.assertEqual(
                note.next_attempt_at, t1 + timedelta(minutes=30)
            )  # from the failed retry
            t2 = t1 + timedelta(minutes=30)
            self.assertEqual(cs.send_due(t2 - timedelta(seconds=1)), {})
            cs.send_due(t2)
            note.refresh_from_db()
            self.assertEqual(note.next_attempt_at, t2 + timedelta(hours=2))
            t3 = t2 + timedelta(hours=2)
            cs.send_due(t3)
            note.refresh_from_db()
            self.assertEqual(
                (note.status, note.attempt_count, note.next_attempt_at), (S.FAILED, 4, None)
            )
            self.assertIn("SMTPException: boom", note.last_error)
            self.assertEqual(cs.send_due(t3 + timedelta(days=30)), {})  # no further retrying
        self.assertEqual(note.attempts.count(), 4)
        self.assertTrue(all(not a.accepted and "boom" in a.error for a in note.attempts.all()))
        self.assertEqual(len(mail.outbox), 0)

    def test_a_retry_that_succeeds_reuses_the_record_and_sends_once(self):
        t0 = ist(2026, 11, 11, 10)
        with fail_smtp():
            cs.send_due(t0)
        before = N.objects.count()
        cs.send_due(t0 + timedelta(minutes=5))
        note = N.objects.get(pk=self.receipt.pk)
        self.assertEqual(
            (note.status, note.attempt_count, note.last_error), (S.SMTP_ACCEPTED, 2, "")
        )
        self.assertEqual(N.objects.count(), before)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual([a.accepted for a in note.attempts.all()], [False, True])

    def test_the_address_is_re_resolved_at_send_time_and_a_removed_address_skips(self):
        t0 = ist(2026, 11, 11, 10)
        give_email(self.member, "")
        cs.send_due(t0)
        note = N.objects.get(pk=self.receipt.pk)
        self.assertEqual((note.status, note.skip_code), (S.SKIPPED, "no_email"))
        self.assertEqual(len(mail.outbox), 0)

    def test_anonymising_between_failure_and_retry_stops_the_email(self):
        t0 = ist(2026, 11, 11, 10)
        with fail_smtp():
            cs.send_due(t0)
        members.anonymise_member(self.member, actor=self.owner, reason="r", confirm=True)
        cs.send_due(t0 + timedelta(minutes=5))
        note = N.objects.get(pk=self.receipt.pk)
        self.assertEqual(
            (note.status, note.skip_code, note.body, note.recipient_email),
            (S.SKIPPED, "anonymised", "", ""),
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_newlines_in_a_title_cannot_inject_headers(self):
        from circulation.tests.helpers import make_copies

        copy = make_copies(self.owner, 1, title="Bad\r\nBcc: evil@example.com")[0]
        services.issue_loan(actor=self.owner, member=self.member, copy=copy, today=D(2026, 12, 1))
        cs.run_cycle(ist(2026, 12, 1, 12))
        message = [m for m in mail.outbox if "Bad" in m.subject][0]
        self.assertNotIn("\n", message.subject)
        self.assertEqual(message.bcc, [])

    def test_secrets_are_scrubbed_from_stored_errors(self):
        with override_settings(EMAIL_HOST_PASSWORD="hunter2-secret"):
            with mock.patch.object(
                cs.EmailMessage, "send", side_effect=OSError("auth hunter2-secret failed")
            ):
                cs.send_due(ist(2026, 11, 11, 10))
        error = N.objects.get(pk=self.receipt.pk).last_error
        self.assertNotIn("hunter2-secret", error)
        self.assertIn("***", error)

    def test_lease_blocks_a_second_claim_until_it_expires_and_a_lost_claim_cannot_record(self):
        t0 = ist(2026, 11, 11, 10)
        token1, ids1 = cs.claim_due(t0)
        self.assertEqual(len(ids1), 2)
        token2, ids2 = cs.claim_due(t0 + timedelta(minutes=1))
        self.assertEqual(ids2, [])  # still leased
        token3, ids3 = cs.claim_due(t0 + cs.LEASE + timedelta(seconds=1))  # crashed worker
        self.assertEqual(sorted(ids3), sorted(ids1))
        self.assertIsNone(cs.attempt(ids1[0], token1, t0))  # the stale claim records nothing
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(cs.attempt(ids3[0], token3, t0), S.SMTP_ACCEPTED)


class DatabaseRuleTests(TestCase):
    def setUp(self):
        owner = make_owner()
        give_email(member_at(owner))
        cs.run_cycle(ist(2026, 11, 11, 10))
        self.done = N.objects.filter(status=S.SMTP_ACCEPTED).first()
        self.skipped = N.objects.filter(status=S.SKIPPED).first()

    def refused(self, sql, params=()):
        with self.assertRaises(DatabaseError):
            with transaction.atomic(), connection.cursor() as cur:
                cur.execute(sql, params)

    def test_finished_notifications_and_attempts_cannot_change_or_be_deleted(self):
        self.refused(
            "UPDATE communications_notification SET last_error='x' WHERE id=%s", [self.done.pk]
        )
        self.refused(
            "UPDATE communications_notification SET status='prepared' WHERE id=%s", [self.done.pk]
        )
        self.refused(
            "UPDATE communications_notification SET subject='x' WHERE id=%s", [self.skipped.pk]
        )
        self.refused("DELETE FROM communications_notification WHERE id=%s", [self.done.pk])
        self.refused("UPDATE communications_notificationattempt SET error='x'")
        self.refused("DELETE FROM communications_notificationattempt")

    def test_identity_never_changes_even_while_open(self):
        n = N.objects.create(
            dedupe_key="x:1",
            source="schedule",
            kind="loan_issued",
            member=self.done.member,
            status="prepared",
        )
        for col, val in (
            ("dedupe_key", "'y'"),
            ("kind", "'loan_returned'"),
            ("source", "'event'"),
            ("member_id", "999"),
        ):
            self.refused(f"UPDATE communications_notification SET {col}={val} WHERE id=%s", [n.pk])

    def test_open_states_only_move_forward(self):
        n = N.objects.create(
            dedupe_key="x:2",
            source="schedule",
            kind="loan_issued",
            member=self.done.member,
            status="prepared",
            attempt_count=2, recipient_email="a@example.com",
        )  # fmt: skip
        N.objects.filter(pk=n.pk).update(status="smtp_failed")
        self.refused("UPDATE communications_notification SET status='prepared' WHERE id=%s", [n.pk])
        self.refused("UPDATE communications_notification SET attempt_count=1 WHERE id=%s", [n.pk])
        N.objects.filter(pk=n.pk).update(
            status="skipped", skip_code="no_email"
        )  # never accepted: allowed

    def test_constraints(self):
        base = dict(source="schedule", kind="loan_issued", member=self.done.member)
        for bad in (
            dict(dedupe_key=""),
            dict(dedupe_key="c:1", attempt_count=5),
            dict(dedupe_key="c:2", status="skipped"),  # a skip needs a code
            dict(dedupe_key="c:3", status="smtp_accepted"),  # needs time and address
            dict(dedupe_key="c:4", status="delivered"),
            dict(dedupe_key=self.done.dedupe_key),  # duplicate key
        ):
            with self.assertRaises(IntegrityError), transaction.atomic():
                N.objects.create(**base, **bad)
        with self.assertRaises(IntegrityError), transaction.atomic():
            NotificationAttempt.objects.create(notification=self.done, attempt_no=1, accepted=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            NotificationAttempt.objects.create(notification=self.done, attempt_no=9, accepted=False)

    def test_trigger_function_search_path_is_pinned(self):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT proconfig FROM pg_proc WHERE proname='communications_notification_guard'"
            )
            self.assertIn("search_path=pg_catalog", cur.fetchone()[0])


class ConcurrencyTests(TransactionTestCase):
    def setUp(self):
        from memberships.tests.helpers import ensure_plans

        ensure_plans()
        self.owner = make_owner()
        give_email(member_at(self.owner))

    def run_threads(self, target, n=4):
        barrier = threading.Barrier(n)
        results = [None] * n

        def work(i):
            try:
                barrier.wait()
                results[i] = target()
            finally:
                connection.close()

        threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    def test_concurrent_consumers_create_one_record_per_event(self):
        events = DomainEvent.objects.count()
        self.run_threads(lambda: cs.consume_events(ist(2026, 11, 11)))
        self.assertEqual(N.objects.count(), events)
        self.assertEqual(len(set(N.objects.values_list("dedupe_key", flat=True))), events)

    def test_concurrent_senders_send_each_email_once(self):
        cs.consume_events(ist(2026, 11, 11))
        open_count = N.objects.filter(status=S.PREPARED).count()
        self.run_threads(lambda: cs.send_due(ist(2026, 11, 11, 10), limit=1))
        self.run_threads(lambda: cs.send_due(ist(2026, 11, 11, 10)))
        self.assertEqual(len(mail.outbox), open_count)
        self.assertEqual(N.objects.filter(status=S.SMTP_ACCEPTED).count(), open_count)
        for note in N.objects.filter(status=S.SMTP_ACCEPTED):
            self.assertEqual(note.attempts.count(), 1)


def keys(now, grace=7):
    return sorted(k for k, *_ in cs.due_reminders(now, grace))


class ReminderTests(TestCase):
    def setUp(self):
        self.owner = make_owner()

    def test_nothing_is_created_before_0900_ist_and_the_boundary_is_exact(self):
        member_at(self.owner)  # expires 11 May 2027 -> reminder on 26 April
        self.assertEqual(cs.schedule_reminders(ist(2027, 4, 26, 8, 59, 59)), 0)
        self.assertEqual(cs.schedule_reminders(ist(2027, 4, 26, 9, 0, 0)), 1)

    def test_the_clock_is_asia_kolkata_not_utc(self):
        member_at(self.owner)
        utc = dt_timezone.utc
        self.assertEqual(cs.schedule_reminders(ist(2027, 4, 26, 8, 59).astimezone(utc)), 0)
        self.assertEqual(cs.schedule_reminders(ist(2027, 4, 26, 9, 0).astimezone(utc)), 1)

    def test_expiry_reminder_is_15_days_before_and_notice_on_the_expiry_date(self):
        sub = member_at(self.owner).subscriptions.get()  # expires 11 May 2027
        self.assertEqual(keys(ist(2027, 4, 25)), [])
        self.assertEqual(keys(ist(2027, 4, 26)), [f"reminder:expiry:{sub.pk}"])
        self.assertEqual(keys(ist(2027, 5, 3)), [f"reminder:expiry:{sub.pk}"])  # 7 days late
        self.assertEqual(keys(ist(2027, 5, 4)), [])  # 8 days late: too old
        self.assertEqual(
            keys(ist(2027, 5, 11)), [f"reminder:expired:{sub.pk}"]
        )  # expired: notice only
        self.assertEqual(keys(ist(2027, 5, 11, 8, 59)), [])  # the notice waits for 09:00

    def test_a_renewed_member_gets_no_expiry_reminder_or_notice(self):
        member = member_at(self.owner)
        ms.renew_subscription(
            actor=self.owner, member=member, plan_code="6m",
            entries=[Entry("cash", 80000)], today=D(2027, 4, 1),
        )  # fmt: skip
        for day in (D(2027, 4, 26), D(2027, 5, 11), D(2027, 5, 12)):
            self.assertEqual(keys(ist(day.year, day.month, day.day)), [], day)

    def test_a_late_renewal_after_expiry_cancels_the_pending_notice(self):
        member = member_at(self.owner)
        ms.renew_subscription(
            actor=self.owner, member=member, plan_code="6m",
            entries=[Entry("cash", 80000)], today=D(2027, 5, 12), activation_date=D(2027, 5, 12),
        )  # fmt: skip
        self.assertEqual(keys(ist(2027, 5, 12)), [])  # covered again: not expired any more

    def test_overdue_reminders_on_due_date_day_3_and_day_7_only(self):
        member, copy, loan = overdue_loan(self.owner)  # due 31 Dec 2026, day 8 = 8 Jan
        got = {}
        for offset in range(0, 10):
            day = D(2026, 12, 31) + timedelta(days=offset)
            got[offset] = keys(ist(day.year, day.month, day.day))
        self.assertEqual(got[0], [f"reminder:overdue:{loan.pk}:0"])
        self.assertEqual(got[3], [f"reminder:overdue:{loan.pk}:3"])
        self.assertEqual(got[7], [f"reminder:overdue:{loan.pk}:7"])
        for offset in (8, 9):
            self.assertEqual(got[offset], [], offset)  # day 8: the forfeiture is the notice
        services.return_loan(actor=self.owner, loan=loan, today=D(2027, 1, 1))
        self.assertEqual(keys(ist(2027, 1, 3)), [])

    def test_reminders_are_created_once_and_missing_emails_are_recorded_as_skipped(self):
        overdue_loan(self.owner)
        now = ist(2027, 1, 3, 9, 30)
        self.assertEqual(cs.schedule_reminders(now), 1)
        self.assertEqual(cs.schedule_reminders(now), 0)
        self.assertEqual(cs.schedule_reminders(ist(2027, 1, 3, 17)), 0)
        note = Notification.objects.get(source="schedule")
        self.assertEqual((note.status, note.skip_code), (S.SKIPPED, "no_email"))

    def test_overdue_email_wording_and_idempotent_send(self):
        member, copy, loan = overdue_loan(self.owner)
        give_email(member)
        cs.run_cycle(ist(2027, 1, 3, 9, 30))
        late = [m for m in mail.outbox if m.subject.startswith("Overdue book")]
        self.assertEqual(len(late), 1)
        self.assertIn("3 days overdue", late[0].body)
        self.assertIn("8 January 2027", late[0].body)
        cs.run_cycle(ist(2027, 1, 3, 9, 45))
        self.assertEqual(len([m for m in mail.outbox if m.subject.startswith("Overdue book")]), 1)
        cs.run_cycle(ist(2026, 12, 31, 9, 5))  # the due date itself: "due today"
        self.assertTrue(any(m.subject.startswith("Book due today") for m in mail.outbox))

    def test_anonymised_members_get_no_reminders(self):
        member = member_at(self.owner)
        members.anonymise_member(member, actor=self.owner, reason="r", confirm=True)
        self.assertEqual(keys(ist(2027, 4, 26)), [])


class BackfillTests(TestCase):
    """D-25: a missed reminder is created later, for up to 7 days, only while still relevant."""

    def setUp(self):
        self.owner = make_owner()

    def test_a_missed_expiry_reminder_is_created_on_the_next_run_with_its_original_date(self):
        member = give_email(member_at(self.owner))  # expires 11 May 2027, reminder due 26 April
        late = ist(2027, 4, 29, 11, 15)  # the Dell was off for three days
        self.assertEqual(cs.schedule_reminders(late), 1)
        note = Notification.objects.get(source="schedule")
        self.assertEqual(note.scheduled_for, D(2027, 4, 26))  # the original scheduled date
        self.assertEqual(note.created_at, late)  # the real processing time, kept apart
        self.assertEqual(cs.schedule_reminders(ist(2027, 4, 30, 9, 5)), 0)  # no duplicate later
        self.assertEqual(member.notifications.filter(source="schedule").count(), 1)

    def test_backfill_stops_after_7_days_but_a_reminder_still_relevant_is_not_stale_by_age(self):
        member_at(self.owner)
        due = D(2027, 4, 26)
        for age, expected in ((7, 1), (8, 0)):
            day = due + timedelta(days=age)
            self.assertEqual(len(keys(ist(day.year, day.month, day.day, 12))), expected, age)
        # a notice for an expiry 7 days ago is still created, 8 days ago it is not
        notice = {
            a: len([k for k in keys(ist(2027, 5, 11 + a, 12)) if "expired" in k]) for a in (7, 8)
        }
        self.assertEqual(notice, {7: 1, 8: 0})

    def test_expiry_reminder_not_backfilled_once_expired_or_renewed(self):
        member = member_at(self.owner)
        self.assertEqual([k for k in keys(ist(2027, 5, 11, 12)) if "expiry:" in k], [])  # expired
        ms.renew_subscription(
            actor=self.owner, member=member, plan_code="6m",
            entries=[Entry("cash", 80000)], today=D(2027, 4, 28),
        )  # fmt: skip
        self.assertEqual(keys(ist(2027, 4, 30, 12)), [])

    def test_only_the_latest_missed_overdue_milestone_is_sent(self):
        member, copy, loan = overdue_loan(self.owner)  # due 31 Dec
        give_email(member)
        # offline since the due date; back on day 5 (5 Jan): day 0 and day 3 were both missed
        self.assertEqual(keys(ist(2027, 1, 5, 10)), [f"reminder:overdue:{loan.pk}:3"])
        cs.run_cycle(ist(2027, 1, 5, 10))
        notes = Notification.objects.filter(source="schedule")
        self.assertEqual(
            [(n.dedupe_key, n.scheduled_for) for n in notes],
            [(f"reminder:overdue:{loan.pk}:3", D(2027, 1, 3))],
        )
        self.assertIn("5 days overdue", mail.outbox[-1].body)  # the real state, not the old day
        cs.run_cycle(ist(2027, 1, 6, 10))
        self.assertEqual(notes.count(), 1)  # the older milestone is not created afterwards

    def test_no_overdue_backfill_from_day_8_or_after_the_loan_closed(self):
        member, copy, loan = overdue_loan(self.owner)
        self.assertEqual(keys(ist(2027, 1, 8, 10)), [])  # day 8 reached
        services.return_loan(actor=self.owner, loan=loan, today=D(2027, 1, 4))
        self.assertEqual(keys(ist(2027, 1, 5, 10)), [])

    def test_backfill_respects_the_grace_setting(self):
        member, copy, loan = overdue_loan(self.owner)
        self.assertEqual(len(keys(ist(2027, 1, 5, 10), grace=7)), 1)
        self.assertEqual(keys(ist(2027, 1, 5, 10), grace=3), [])  # day 8 is already 4 Jan

    def test_run_daily_jobs_is_idempotent_for_reminders(self):
        from io import StringIO

        from django.core.management import call_command

        member, copy, loan = overdue_loan(self.owner)
        give_email(member)
        with mock.patch("django.utils.timezone.now", return_value=ist(2027, 1, 3, 10)):
            for _ in range(3):
                call_command("run_daily_jobs", stdout=StringIO())
        self.assertEqual(Notification.objects.filter(source="schedule").count(), 1)
        self.assertEqual(len([m for m in mail.outbox if m.subject.startswith("Overdue")]), 1)
