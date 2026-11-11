from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts import ratelimit
from accounts.models import LoginEvent, RecoveryCode
from accounts.recovery import consume_code, issue_code
from accounts.sessions import end_of_day, seconds_until_end_of_day
from audit.models import AuditEvent

PW = "Correct-Horse-9!"
KOLKATA = ZoneInfo("Asia/Kolkata")


class AuthBase(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "owner", email="owner@example.com", password=PW
        )

    def login(self, password=PW, username="owner"):
        return self.client.post(
            reverse("accounts:login"), {"username": username, "password": password}
        )

    def fail(self, n, username="owner"):
        for _ in range(n):
            self.login("wrong", username)


class LoginTests(AuthBase):
    def test_login_page_renders_brand_without_sidebar(self):
        r = self.client.get(reverse("accounts:login"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Coven&#x27;s Library")
        self.assertNotContains(r, "Coffee")

    def test_successful_login_logs_and_redirects(self):
        r = self.login()
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(LoginEvent.objects.filter(outcome="success").count(), 1)
        self.assertTrue(AuditEvent.objects.filter(action="auth.login").exists())

    def test_failed_login_is_audited_without_password(self):
        r = self.login("wrong-password-xyz")
        self.assertEqual(r.status_code, 200)
        ev = LoginEvent.objects.get()
        self.assertEqual(ev.outcome, "failed")
        audit = AuditEvent.objects.get(action="auth.login_failed")
        self.assertNotIn("wrong-password-xyz", str(audit.summary))
        self.assertNotIn("wrong-password-xyz", str(ev.__dict__))

    def test_protected_pages_redirect_to_login(self):
        r = self.client.get("/")
        self.assertRedirects(r, "/accounts/login/?next=/", fetch_redirect_response=False)

    def test_logout_requires_post(self):
        self.login()
        self.assertEqual(self.client.get(reverse("accounts:logout")).status_code, 405)
        self.client.post(reverse("accounts:logout"))
        self.assertEqual(self.client.get("/").status_code, 302)


class UnlimitedLoginTests(AuthBase):
    def test_repeated_logins_after_logout_and_lost_session(self):
        for _ in range(3):
            self.assertEqual(self.login().status_code, 302)
            self.client.post(reverse("accounts:logout"))
        self.assertEqual(self.login().status_code, 302)
        self.client.cookies.clear()  # lost session
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.login().status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)


class SessionExpiryTests(AuthBase):
    def test_end_of_day_is_next_local_midnight(self):
        now = datetime(2026, 11, 11, 21, 30, tzinfo=KOLKATA)
        self.assertEqual(end_of_day(now), datetime(2026, 11, 12, 0, 0, tzinfo=KOLKATA))
        self.assertEqual(seconds_until_end_of_day(now), 2.5 * 3600)

    def test_end_of_day_uses_kolkata_not_utc(self):
        # 20:00 UTC is 01:30 next day in Kolkata: expiry must be the following Kolkata midnight.
        now = datetime(2026, 11, 11, 20, 0, tzinfo=dt_timezone.utc)
        self.assertEqual(end_of_day(now), datetime(2026, 11, 13, 0, 0, tzinfo=KOLKATA))

    def test_session_expires_at_end_of_day_and_relogin_works(self):
        self.login()
        age = self.client.session.get_expiry_age()
        self.assertLessEqual(age, 24 * 3600)
        self.assertGreater(age, 0)
        expiry = self.client.session.get_expiry_date()
        local = timezone.localtime(expiry)
        self.assertEqual((local.hour, local.minute), (0, 0))
        # after the expiry the session no longer authenticates, and logging in again works
        future = timezone.now() + timedelta(hours=25)
        with mock.patch("django.utils.timezone.now", return_value=future):
            self.assertEqual(self.client.get("/").status_code, 302)
            self.assertEqual(self.login().status_code, 302)


class RateLimitTests(AuthBase):
    def test_fifth_failure_throttles_even_correct_password(self):
        self.fail(5)
        r = self.login()  # correct password during the throttle window
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r)
        self.assertEqual(LoginEvent.objects.filter(outcome="throttled").count(), 1)
        self.assertTrue(AuditEvent.objects.filter(action="auth.login_throttled").exists())

    def test_four_failures_do_not_throttle(self):
        self.fail(4)
        self.assertEqual(self.login().status_code, 302)

    def test_no_account_lockout_throttle_expires_after_window(self):
        self.fail(5)
        self.assertEqual(self.login().status_code, 429)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        later = timezone.now() + timedelta(minutes=16)
        with mock.patch("django.utils.timezone.now", return_value=later):
            with mock.patch("accounts.ratelimit.timezone.now", return_value=later):
                self.assertIsNone(ratelimit.retry_after("owner", "127.0.0.1", now=later))
        LoginEvent.objects.update(occurred_at=timezone.now() - timedelta(minutes=16))
        self.assertEqual(self.login().status_code, 302)

    def test_success_resets_counter(self):
        self.fail(4)
        self.assertEqual(self.login().status_code, 302)
        self.client.post(reverse("accounts:logout"))
        self.fail(4)  # four more is still below the limit because success reset the count
        self.assertEqual(self.login().status_code, 302)

    def test_throttled_attempts_do_not_extend_window(self):
        self.fail(5)
        for _ in range(4):
            self.assertEqual(self.login().status_code, 429)
        LoginEvent.objects.filter(outcome="failed").update(
            occurred_at=timezone.now() - timedelta(minutes=16)
        )
        self.assertEqual(self.login().status_code, 302)

    def test_retry_after_value(self):
        self.fail(5)
        wait = ratelimit.retry_after("owner", "127.0.0.1")
        self.assertTrue(timedelta(minutes=14) < wait <= timedelta(minutes=15))


class PasswordResetTests(AuthBase):
    def test_reset_email_flow(self):
        r = self.client.post(reverse("accounts:password_reset"), {"email": "owner@example.com"})
        self.assertRedirects(r, reverse("accounts:password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn("Coven's Library", mail.outbox[0].subject)
        link = [w for w in body.split() if "/accounts/reset/" in w][0]
        path = link.split("://", 1)[1].split("/", 1)[1]
        r = self.client.get("/" + path, follow=True)
        self.assertEqual(r.status_code, 200)
        post_url = r.redirect_chain[-1][0] if r.redirect_chain else "/" + path
        r = self.client.post(
            post_url, {"new_password1": "Brand-New-Pass-77!", "new_password2": "Brand-New-Pass-77!"}
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.login("Brand-New-Pass-77!").status_code, 302)

    def test_unknown_email_sends_nothing_but_looks_the_same(self):
        r = self.client.post(reverse("accounts:password_reset"), {"email": "nobody@example.com"})
        self.assertRedirects(r, reverse("accounts:password_reset_done"))
        self.assertEqual(len(mail.outbox), 0)


class RecoveryCodeTests(AuthBase):
    def test_code_is_hashed_single_use_and_expires(self):
        code = issue_code(self.user)
        stored = RecoveryCode.objects.get()
        self.assertNotIn(code.replace("-", ""), stored.code_hash)
        self.assertEqual(stored.expires_at - stored.created_at, timedelta(minutes=15))
        self.assertTrue(consume_code(self.user, code))
        self.assertFalse(consume_code(self.user, code))  # single use

    def test_expired_code_rejected(self):
        code = issue_code(self.user)
        later = timezone.now() + timedelta(minutes=16)
        self.assertFalse(consume_code(self.user, code, now=later))
        self.assertFalse(consume_code(self.user, "WRONG-CODE1"))

    def test_recovery_page_resets_password_and_audits(self):
        code = issue_code(self.user)
        r = self.client.post(
            reverse("accounts:recover"),
            {
                "username": "owner",
                "code": code,
                "new_password1": "Recovered-Pass-55!",
                "new_password2": "Recovered-Pass-55!",
            },
        )
        self.assertRedirects(r, reverse("accounts:login"))
        self.assertEqual(self.login("Recovered-Pass-55!").status_code, 302)
        self.assertTrue(AuditEvent.objects.filter(action="auth.password_recovered").exists())
        self.assertFalse(AuditEvent.objects.filter(summary__icontains=code).exists())

    def test_wrong_code_fails_and_is_rate_limited(self):
        issue_code(self.user)
        data = {
            "username": "owner",
            "code": "AAAAA-BBBBB",
            "new_password1": "Another-Pass-1!",
            "new_password2": "Another-Pass-1!",
        }
        for _ in range(5):
            self.assertEqual(self.client.post(reverse("accounts:recover"), data).status_code, 200)
        self.assertEqual(self.client.post(reverse("accounts:recover"), data).status_code, 429)
        self.assertEqual(LoginEvent.objects.filter(outcome="recovery_failed").count(), 5)

    def test_management_command_prints_code_once(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("issue_recovery_code", "--username", "owner", stdout=out)
        self.assertIn("single use", out.getvalue())
        self.assertEqual(RecoveryCode.objects.count(), 1)
