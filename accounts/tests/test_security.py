import json
import re
import threading
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from accounts import ratelimit
from accounts.identity import audit_username, key_for, known_username
from accounts.models import LoginEvent
from accounts.recovery import consume_code, issue_code
from audit.models import AuditEvent

PW = "Correct-Horse-9!"
TYPED_PASSWORD = "Sup3r-Secret-Typed-Password!"


def everything_stored():
    """All text persisted by authentication and the audit trail, as one searchable string."""
    rows = list(LoginEvent.objects.values()) + list(AuditEvent.objects.values())
    return json.dumps(rows, default=str)


class TypedPasswordTests(TestCase):
    """M4: a password typed into the username box must never be stored."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "owner", email="o@example.com", password=PW
        )

    def test_login_with_password_in_username_field_stores_nothing_sensitive(self):
        self.client.post(reverse("accounts:login"), {"username": TYPED_PASSWORD, "password": "x"})
        ev = LoginEvent.objects.get()
        self.assertEqual(ev.outcome, "failed")
        self.assertEqual(ev.username_tried, "")
        self.assertEqual(ev.username_key, key_for(TYPED_PASSWORD))
        self.assertNotIn(TYPED_PASSWORD, everything_stored())
        self.assertEqual(
            AuditEvent.objects.get(action="auth.login_failed").summary,
            {"username": "[unrecognised]"},
        )

    def test_throttled_and_recovery_paths_also_store_nothing_sensitive(self):
        for _ in range(5):
            self.client.post(
                reverse("accounts:login"), {"username": TYPED_PASSWORD, "password": "x"}
            )
        r = self.client.post(
            reverse("accounts:login"), {"username": TYPED_PASSWORD, "password": "x"}
        )
        self.assertEqual(r.status_code, 429)
        self.assertTrue(LoginEvent.objects.filter(outcome="throttled").exists())
        Client().post(
            reverse("accounts:recover"),
            {
                "username": TYPED_PASSWORD,
                "code": "AAAAA-BBBBB",
                "new_password1": "Another-Pass-1!",
                "new_password2": "Another-Pass-1!",
            },
        )
        self.assertNotIn(TYPED_PASSWORD, everything_stored())

    def test_known_username_is_stored_canonically(self):
        self.client.post(reverse("accounts:login"), {"username": "OWNER", "password": "wrong"})
        ev = LoginEvent.objects.get()
        self.assertEqual((ev.username_tried, ev.username_key), ("owner", key_for("owner")))
        self.assertEqual(AuditEvent.objects.get().summary, {"username": "owner"})

    def test_key_is_keyed_and_not_the_plaintext(self):
        k = key_for(TYPED_PASSWORD)
        self.assertEqual(len(k), 64)
        self.assertNotIn(TYPED_PASSWORD.lower(), k)
        self.assertEqual(k, key_for("  " + TYPED_PASSWORD.upper() + " "))  # normalised
        with mock.patch.object(settings, "SECRET_KEY", "another-secret-key" * 3):
            self.assertNotEqual(k, key_for(TYPED_PASSWORD))
        self.assertEqual(
            (known_username(TYPED_PASSWORD), audit_username(TYPED_PASSWORD)), ("", "[unrecognised]")
        )

    def test_oversized_username_is_not_looked_up_or_stored(self):
        self.client.post(reverse("accounts:login"), {"username": "x" * 500, "password": "x"})
        self.assertNotIn("x" * 200, everything_stored())


class ConcurrentRateLimitTests(TransactionTestCase):
    """M2: simultaneous failed logins cannot exceed the 5-in-15-minutes limit."""

    def test_limit_holds_under_simultaneous_failures(self):
        get_user_model().objects.create_user("target", password=PW)
        n = 24
        barrier = threading.Barrier(n)
        statuses = []

        def attempt():
            client = Client()
            barrier.wait()
            try:
                r = client.post(
                    reverse("accounts:login"),
                    {"username": "target", "password": "bad"},
                    REMOTE_ADDR="10.1.2.3",
                )
                statuses.append(r.status_code)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=attempt) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        failed = LoginEvent.objects.filter(outcome="failed").count()
        self.assertEqual(failed, ratelimit.MAX_FAILURES)
        self.assertEqual(statuses.count(200), ratelimit.MAX_FAILURES)
        self.assertEqual(statuses.count(429), n - ratelimit.MAX_FAILURES)
        self.assertEqual(LoginEvent.objects.filter(outcome="throttled").count(), n - 5)
        # no lockout: the account is untouched and works once the window has passed
        self.assertTrue(get_user_model().objects.get(username="target").is_active)
        LoginEvent.objects.filter(outcome="failed").update(
            occurred_at=timezone.now() - timedelta(minutes=16)
        )
        r = Client().post(
            reverse("accounts:login"),
            {"username": "target", "password": PW},
            REMOTE_ADDR="10.1.2.3",
        )
        self.assertEqual(r.status_code, 302)

    def test_concurrent_attempts_for_different_users_share_the_ip_limit(self):
        get_user_model().objects.create_user("owner", password=PW)
        n = 16
        barrier = threading.Barrier(n)
        statuses = []

        def attempt(i):
            client = Client()
            barrier.wait()
            try:
                r = client.post(
                    reverse("accounts:login"),
                    {"username": f"guess{i}", "password": "x"},
                    REMOTE_ADDR="10.7.7.7",
                )
                statuses.append(r.status_code)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(LoginEvent.objects.filter(outcome="failed").count(), 5)

    def test_advisory_lock_requires_a_transaction(self):
        from accounts.locks import advisory_locks

        with self.assertRaises(RuntimeError):
            advisory_locks("x")
        self.assertTrue(connection.vendor == "postgresql")


class PasswordResetTests(TestCase):
    """M3: reset requests and completions are audited and throttled."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "owner", email="o@example.com", password=PW
        )

    def request_reset(self, email="o@example.com", ip="127.0.0.1"):
        return self.client.post(
            reverse("accounts:password_reset"), {"email": email}, REMOTE_ADDR=ip
        )

    # ---- helpers for the link-lifetime tests -------------------------------------------------
    def reset_link_path(self):
        link = [w for w in mail.outbox[-1].body.split() if "/accounts/reset/" in w][0]
        return "/" + link.split("://", 1)[1].split("/", 1)[1]

    def at(self, offset_seconds, fn):
        """Run fn() as if `offset_seconds` had passed since the link was generated."""
        from datetime import datetime

        with mock.patch(
            "django.contrib.auth.tokens.PasswordResetTokenGenerator._now",
            return_value=datetime.now() + timedelta(seconds=offset_seconds),
        ):
            return fn()

    def test_lifetime_is_exactly_one_hour(self):
        self.assertEqual(settings.PASSWORD_RESET_TIMEOUT, 3600)
        self.request_reset()
        path = self.reset_link_path()
        fresh = self.at(5, lambda: Client().get(path, follow=True))
        self.assertContains(fresh, "Choose a new password")
        self.assertNotContains(fresh, "invalid or has expired")
        just_before = self.at(3600 - 10, lambda: Client().get(path, follow=True))
        self.assertNotContains(just_before, "invalid or has expired")
        just_after = self.at(3600 + 10, lambda: Client().get(path, follow=True))
        self.assertContains(just_after, "invalid or has expired")
        three_days = self.at(259200 - 60, lambda: Client().get(path, follow=True))
        self.assertContains(three_days, "invalid or has expired")  # the old default no longer works

    def test_expired_link_cannot_reset_the_password(self):
        self.request_reset()
        path = self.reset_link_path()
        new_pw = "Expired-Link-Pass-31!"
        # 1) the original link, used after expiry
        r = self.at(
            3600 + 10,
            lambda: self.client.post(
                path, {"new_password1": new_pw, "new_password2": new_pw}, follow=True
            ),
        )
        self.assertContains(r, "invalid or has expired")
        # 2) a link opened in time, then submitted after expiry
        client = Client()
        page = client.get(path, follow=True)
        self.assertContains(page, "Choose a new password")
        form_url = page.redirect_chain[-1][0]
        r = self.at(
            3600 + 10,
            lambda: client.post(
                form_url, {"new_password1": new_pw, "new_password2": new_pw}, follow=True
            ),
        )
        self.assertContains(r, "invalid or has expired")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PW))
        self.assertFalse(self.user.check_password(new_pw))
        self.assertFalse(AuditEvent.objects.filter(action="auth.password_reset_completed").exists())
        self.assertNotIn(new_pw, everything_stored())

    def test_used_link_cannot_be_reused(self):
        self.request_reset()
        path = self.reset_link_path()
        client = Client()
        page = client.get(path, follow=True)
        form_url = page.redirect_chain[-1][0]
        first = "First-Reset-Pass-41!"
        done = client.post(form_url, {"new_password1": first, "new_password2": first})
        self.assertEqual(done.status_code, 302)
        second = "Second-Reset-Pass-52!"
        again_get = Client().get(path, follow=True)
        self.assertContains(again_get, "invalid or has expired")
        again_post = client.post(
            form_url, {"new_password1": second, "new_password2": second}, follow=True
        )
        self.assertContains(again_post, "invalid or has expired")
        fresh_client_post = Client().post(
            path, {"new_password1": second, "new_password2": second}, follow=True
        )
        self.assertContains(fresh_client_post, "invalid or has expired")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(first))
        self.assertFalse(self.user.check_password(second))
        self.assertEqual(
            AuditEvent.objects.filter(action="auth.password_reset_completed").count(), 1
        )

    def test_account_enumeration_remains_prevented(self):
        known = self.request_reset("o@example.com", ip="10.0.0.1")
        unknown = self.request_reset("nobody@example.com", ip="10.0.0.2")
        self.assertEqual(
            (known.status_code, known["Location"]), (unknown.status_code, unknown["Location"])
        )
        page_known = self.client.get(known["Location"]).content
        page_unknown = self.client.get(unknown["Location"]).content
        self.assertEqual(page_known, page_unknown)
        # throttled requests look identical to normal ones as well
        for _ in range(ratelimit.MAX_RESET_REQUESTS + 1):
            throttled = self.request_reset("o@example.com", ip="10.0.0.3")
        self.assertEqual(
            (throttled.status_code, throttled["Location"]), (known.status_code, known["Location"])
        )
        self.assertEqual(self.client.get(throttled["Location"]).content, page_known)
        # an invalid link and an expired link give the same page
        bad = self.client.get("/accounts/reset/MQ/not-a-real-token/", follow=True)
        self.request_reset("o@example.com", ip="10.0.0.9")
        path = self.reset_link_path()
        expired = self.at(3600 + 10, lambda: Client().get(path, follow=True))
        self.assertEqual(bad.content, expired.content)

    def test_throttle_is_still_three_requests_per_fifteen_minutes(self):
        self.assertEqual(ratelimit.MAX_RESET_REQUESTS, 3)
        self.assertEqual(ratelimit.RESET_WINDOW, timedelta(minutes=15))
        sent_before = len(mail.outbox)
        for _ in range(5):
            self.request_reset(ip="10.9.8.7")
        self.assertEqual(len(mail.outbox) - sent_before, 3)

    def test_whole_flow_leaves_no_address_token_password_or_link_in_records(self):
        self.request_reset()
        path = self.reset_link_path()
        uid, token = path.rstrip("/").split("/")[-2:]
        client = Client()
        page = client.get(path, follow=True)
        form_url = page.redirect_chain[-1][0]
        self.at(3600 + 10, lambda: Client().get(path, follow=True))  # an expired attempt
        new_pw = "Whole-Flow-Pass-63!"
        client.post(form_url, {"new_password1": new_pw, "new_password2": new_pw})
        Client().get(path, follow=True)  # a reuse attempt
        stored = everything_stored()
        for secret in (
            "o@example.com",
            uid,
            token,
            path,
            form_url,
            new_pw,
            "/accounts/reset/",
            "set-password",
        ):
            self.assertNotIn(secret, stored, secret)
        self.assertIn("auth.password_reset_requested", stored)
        self.assertIn("auth.password_reset_completed", stored)

    def test_successful_request_is_audited_without_sensitive_data(self):
        r = self.request_reset()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        ev = AuditEvent.objects.get(action="auth.password_reset_requested")
        self.assertEqual(ev.summary, {"accounts_matched": 1})
        self.assertEqual(LoginEvent.objects.get().outcome, "reset_requested")
        stored = everything_stored()
        self.assertNotIn("o@example.com", stored)
        token = re.search(r"/reset/[^/]+/([^/\s]+)/", mail.outbox[0].body).group(1)
        self.assertNotIn(token, stored)

    def test_unknown_email_is_audited_and_sends_nothing(self):
        r = self.request_reset("nobody@example.com")
        self.assertRedirects(r, reverse("accounts:password_reset_done"))
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(
            AuditEvent.objects.get(action="auth.password_reset_requested").summary,
            {"accounts_matched": 0},
        )
        self.assertNotIn("nobody@example.com", everything_stored())

    def test_repeated_requests_are_throttled_with_identical_response(self):
        responses = [self.request_reset() for _ in range(ratelimit.MAX_RESET_REQUESTS + 4)]
        self.assertEqual(len(mail.outbox), ratelimit.MAX_RESET_REQUESTS)
        self.assertTrue(
            all(
                r.status_code == 302 and r["Location"] == responses[0]["Location"]
                for r in responses
            )
        )
        self.assertEqual(LoginEvent.objects.filter(outcome="reset_throttled").count(), 4)
        self.assertEqual(
            AuditEvent.objects.filter(action="auth.password_reset_throttled").count(), 4
        )

    def test_throttle_applies_per_address_across_different_emails(self):
        for i in range(ratelimit.MAX_RESET_REQUESTS):
            self.request_reset(f"user{i}@example.com", ip="10.0.0.9")
        before = len(mail.outbox)
        self.request_reset("o@example.com", ip="10.0.0.9")
        self.assertEqual(len(mail.outbox), before)  # same address: throttled even for the owner

    def test_throttle_expires_and_does_not_extend_the_window(self):
        for _ in range(ratelimit.MAX_RESET_REQUESTS + 2):
            self.request_reset()
        LoginEvent.objects.update(occurred_at=timezone.now() - timedelta(minutes=16))
        before = len(mail.outbox)
        self.request_reset()
        self.assertEqual(len(mail.outbox), before + 1)

    def test_completed_reset_is_audited_without_token_or_password(self):
        self.request_reset()
        link = [w for w in mail.outbox[0].body.split() if "/accounts/reset/" in w][0]
        path = "/" + link.split("://", 1)[1].split("/", 1)[1]
        page = self.client.get(path, follow=True)
        post_url = page.redirect_chain[-1][0] if page.redirect_chain else path
        new_pw = "Brand-New-Pass-77!"
        r = self.client.post(post_url, {"new_password1": new_pw, "new_password2": new_pw})
        self.assertEqual(r.status_code, 302)
        ev = AuditEvent.objects.get(action="auth.password_reset_completed")
        self.assertEqual((ev.actor, ev.entity_id), (self.user, str(self.user.pk)))
        stored = everything_stored()
        self.assertNotIn(new_pw, stored)
        token = re.search(r"/reset/[^/]+/([^/\s]+)/", mail.outbox[0].body).group(1)
        self.assertNotIn(token, stored)
        self.assertEqual(
            self.client.post(
                reverse("accounts:login"), {"username": "owner", "password": new_pw}
            ).status_code,
            302,
        )

    def test_failed_reset_attempt_with_bad_link_writes_no_completion_event(self):
        self.client.get("/accounts/reset/MQ/not-a-real-token/")
        self.assertFalse(AuditEvent.objects.filter(action="auth.password_reset_completed").exists())

    def test_invalid_email_address_is_rejected_by_the_form(self):
        r = self.client.post(reverse("accounts:password_reset"), {"email": "not-an-email"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(LoginEvent.objects.count(), 0)


class RecoveryCodeSupersessionTests(TestCase):
    def test_new_code_invalidates_earlier_unused_code(self):
        user = get_user_model().objects.create_user("owner", password=PW)
        first = issue_code(user)
        second = issue_code(user)
        self.assertFalse(consume_code(user, first))
        self.assertTrue(consume_code(user, second))
