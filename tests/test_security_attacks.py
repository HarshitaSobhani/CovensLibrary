"""Security audit probes (docs/SECURITY_AUDIT.md). Each test ATTACKS the running application through
the Django test client (and the real middleware stack) and asserts what actually happens; none
relies on a setting merely existing. Windows-only behaviour is not covered here."""

import re
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import get_resolver, reverse
from django.utils import timezone

from audit.models import AuditEvent
from catalogue import services as cat
from catalogue.models import Category
from finance.models import FinancialTransaction
from members import services as members
from memberships.tests.helpers import PW, make_owner
from tests.test_access import iter_patterns

XSS = [
    '<script>alert("x1")</script>',
    '"><img src=x onerror=alert("x2")>',
    "'><svg/onload=alert('x3')>",
    "{{7*7}}{% load x %}",
]
PUBLIC = {
    "accounts:login",
    "accounts:password_reset",
    "accounts:password_reset_done",
    "accounts:password_reset_confirm",
    "accounts:password_reset_complete",
    "accounts:recover",
    "core:healthz",  # the launcher's loopback health probe; it answers only {"app", "status"}
}


def concrete(route):
    """A requestable URL for a route pattern (path parameters replaced with plausible values)."""
    return "/" + re.sub(r"<int:\w+>", "1", re.sub(r"<str:\w+>", "x", route))


def all_routes():
    for name, route, _ in iter_patterns(get_resolver().url_patterns):
        yield name, concrete(route)


class Unauthenticated(TestCase):
    def test_every_route_with_every_method_refuses_an_anonymous_visitor(self):
        """Threat 1: no data and no operation without the owner's session."""
        client = Client(enforce_csrf_checks=False)
        checked = 0
        for name, url in all_routes():
            if name in PUBLIC or name == "accounts:logout":
                continue
            for method in ("get", "post", "put", "delete"):
                response = getattr(client, method)(url, {"x": "1"})
                self.assertIn(response.status_code, (302, 403, 405), (method, url))
                if response.status_code == 302:
                    self.assertIn("/accounts/login/", response["Location"], (method, url))
                checked += 1
        self.assertGreater(checked, 150)

    def test_the_anonymous_pages_reveal_nothing_about_accounts(self):
        make_owner()
        body = self.client.get(reverse("accounts:login")).content.decode()
        self.assertNotIn("owner", body.replace("Owner", ""))  # no username hint
        # a failed login does not say whether the user exists
        wrong_user = self.client.post(
            reverse("accounts:login"), {"username": "nobody", "password": "x"}
        )
        wrong_pass = self.client.post(
            reverse("accounts:login"), {"username": "owner", "password": "x"}
        )
        strip = lambda r: re.sub(r' value="[^"]*"', "", r.content.decode())  # noqa: E731
        self.assertEqual(strip(wrong_user), strip(wrong_pass))
        self.assertEqual(wrong_user.status_code, wrong_pass.status_code)

    def test_the_public_pages_do_not_leak_server_details(self):
        response = self.client.get(reverse("accounts:login"))
        for header in ("Server", "X-Powered-By", "X-Django-Version", "Via"):
            self.assertNotIn(header, response)

    def test_media_dotfiles_and_source_are_not_served(self):
        for url in ("/media/", "/.env", "/.git/config", "/manage.py", "/config/settings/base.py"):
            self.assertEqual(self.client.get(url).status_code, 404, url)


class Csrf(TestCase):
    def test_every_state_changing_route_rejects_a_post_without_the_token(self):
        """Threat 2/3: a forged cross-site POST cannot do anything, even with the owner's cookie."""
        make_owner()
        client = Client(enforce_csrf_checks=True)
        client.login(username="owner", password=PW)
        before = (FinancialTransaction.objects.count(), AuditEvent.objects.count())
        seen = 0
        for name, url in all_routes():
            response = client.post(url, {"x": "1"})
            self.assertEqual(response.status_code, 403, (name, url))
            seen += 1
        self.assertGreater(seen, 50)
        self.assertEqual((FinancialTransaction.objects.count(), AuditEvent.objects.count()), before)

    def test_a_token_from_another_session_is_not_accepted(self):
        make_owner()
        other = Client(enforce_csrf_checks=True)
        other.get(reverse("accounts:login"))
        stolen = other.cookies["csrftoken"].value
        victim = Client(enforce_csrf_checks=True)
        victim.get(reverse("accounts:login"))
        response = victim.post(
            reverse("accounts:login"),
            {"username": "owner", "password": PW, "csrfmiddlewaretoken": stolen},
        )
        self.assertEqual(response.status_code, 403)


class SessionsAndRedirects(TestCase):
    def setUp(self):
        make_owner()

    def test_login_issues_a_new_session_key_so_a_planted_one_is_useless(self):
        client = Client()
        session = client.session
        session["planted"] = True
        session.save()
        planted = client.cookies["sessionid"].value
        client.post(reverse("accounts:login"), {"username": "owner", "password": PW})
        self.assertNotEqual(client.cookies["sessionid"].value, planted)
        replay = Client()
        replay.cookies["sessionid"] = planted
        self.assertEqual(replay.get(reverse("core:dashboard")).status_code, 302)

    def test_logout_ends_the_session_server_side(self):
        client = Client()
        client.post(reverse("accounts:login"), {"username": "owner", "password": PW})
        cookie = client.cookies["sessionid"].value
        self.assertEqual(client.get(reverse("core:dashboard")).status_code, 200)
        client.post(reverse("accounts:logout"))
        replay = Client()
        replay.cookies["sessionid"] = cookie
        self.assertEqual(replay.get(reverse("core:dashboard")).status_code, 302)

    def test_the_session_cookie_cannot_outlive_the_local_day_and_is_httponly_samesite(self):
        client = Client()
        response = client.post(reverse("accounts:login"), {"username": "owner", "password": PW})
        cookie = response.cookies["sessionid"]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        age = int(cookie["max-age"])
        self.assertLessEqual(age, 24 * 3600)
        self.assertGreater(age, 0)
        session = client.session
        self.assertLessEqual(
            session.get_expiry_date(), timezone.now() + timezone.timedelta(hours=24, seconds=5)
        )

    def test_an_expired_session_is_refused(self):
        client = Client()
        client.post(reverse("accounts:login"), {"username": "owner", "password": PW})
        with mock.patch(
            "django.utils.timezone.now", return_value=timezone.now() + timezone.timedelta(days=2)
        ):
            self.assertEqual(client.get(reverse("core:dashboard")).status_code, 302)

    def test_open_redirects_through_next_are_refused(self):
        for target in (
            "https://evil.example/",
            "//evil.example/",
            "///evil.example",
            "/\\evil.example",
            "javascript:alert(1)",
            "https:evil.example",
            "\\\\evil.example",
        ):
            client = Client()
            response = client.post(
                reverse("accounts:login") + "?next=" + target,
                {"username": "owner", "password": PW, "next": target},
            )
            self.assertEqual(response.status_code, 302, target)
            self.assertNotIn("evil", response["Location"], target)
        ok = Client().post(
            reverse("accounts:login") + "?next=/members/",
            {"username": "owner", "password": PW, "next": "/members/"},
        )
        self.assertEqual(ok["Location"], "/members/")

    def test_login_throttle_cannot_be_dodged_with_a_forged_forwarding_header(self):
        for i in range(5):
            self.client.post(
                reverse("accounts:login"),
                {"username": "owner", "password": "bad"},
                HTTP_X_FORWARDED_FOR=f"10.0.0.{i}",
            )
        blocked = self.client.post(
            reverse("accounts:login"),
            {"username": "owner", "password": PW},
            HTTP_X_FORWARDED_FOR="203.0.113.77",
        )
        self.assertEqual(blocked.status_code, 429)
        # the correct password during the window is refused too, and nothing logged the secret
        self.assertFalse(AuditEvent.objects.filter(summary__contains={"password": PW}).exists())

    def test_the_throttle_is_not_a_lockout_and_failures_are_audited(self):
        for _ in range(5):
            self.client.post(reverse("accounts:login"), {"username": "owner", "password": "bad"})
        self.assertGreaterEqual(AuditEvent.objects.filter(action="auth.login_failed").count(), 5)
        self.assertTrue(get_user_model().objects.get(username="owner").is_active)


class PasswordReset(TestCase):
    def setUp(self):
        self.user = make_owner()
        self.user.email = "owner@example.com"
        self.user.save()

    def request(self, email):
        mail.outbox.clear()
        return self.client.post(reverse("accounts:password_reset"), {"email": email})

    def test_an_unknown_address_gets_exactly_the_same_answer_and_no_email(self):
        known = self.request("owner@example.com")
        self.assertEqual(len(mail.outbox), 1)
        unknown = self.request("nobody@example.com")
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(
            (known.status_code, known["Location"]), (unknown.status_code, unknown["Location"])
        )

    def test_the_link_is_single_use_and_the_token_is_not_logged(self):
        self.request("owner@example.com")
        link = re.search(r"http://\S+/reset/\S+/", mail.outbox[0].body).group(0)
        path = link.split("testserver", 1)[1]
        page = self.client.get(path, follow=True)
        self.assertEqual(page.status_code, 200)
        done = self.client.post(
            page.request["PATH_INFO"],
            {"new_password1": "Another-Pass-77!", "new_password2": "Another-Pass-77!"},
        )
        self.assertEqual(done.status_code, 302)
        again = Client().get(path, follow=True)
        self.assertNotContains(again, "new_password1")  # the form is not offered a second time
        token = path.rstrip("/").rsplit("/", 1)[-1]
        self.assertFalse(AuditEvent.objects.filter(summary__icontains=token).exists())

    def test_reset_requests_are_rate_limited(self):
        responses = [self.request("owner@example.com") for _ in range(6)]
        self.assertGreaterEqual(sum(1 for _ in mail.outbox), 0)
        sent = 0
        for _ in range(6):
            mail.outbox.clear()
            self.client.post(reverse("accounts:password_reset"), {"email": "owner@example.com"})
            sent += len(mail.outbox)
        self.assertLessEqual(sent, 3)  # decision: at most 3 per 15 minutes
        self.assertTrue(responses)


class InputAttacks(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.client.login(username="owner", password=PW)
        category = Category.objects.get_or_create(name="Fiction")[0]
        self.member = members.create_member(
            actor=self.owner, full_name=XSS[0], national_number="9876543210", region="IN"
        )
        self.member.notes = XSS[1]
        self.member.save()
        self.edition = cat.create_edition(
            actor=self.owner, title=XSS[1], author=XSS[2], category=category, publisher=XSS[0]
        )

    def pages(self):
        yield reverse("members:list") + "?q=" + XSS[0]
        yield reverse("members:detail", args=[self.member.pk])
        yield reverse("members:edit", args=[self.member.pk])
        yield reverse("catalogue:edition_list") + "?q=" + XSS[1]
        yield reverse("catalogue:edition_detail", args=[self.edition.pk])
        yield reverse("core:search") + "?q=" + XSS[1]
        yield reverse("core:dashboard")
        yield reverse("core:calendar") + "?month=" + XSS[0]
        yield reverse("communications:list") + "?status=" + XSS[2]
        yield (
            reverse("finance:payments") + "?q=" + XSS[0] + "&receipt=" + XSS[1] + "&kind=" + XSS[2]
        )
        yield reverse("finance:payments_tokens") + "?status=" + XSS[1]
        yield reverse("reports:index")
        yield reverse("reports:statement", args=[self.member.pk])
        yield reverse("reports:report", args=["collections"]) + "?preset=" + XSS[0]
        yield reverse("circulation:list") + "?q=" + XSS[0]

    def test_hostile_text_is_escaped_on_every_page_that_shows_it(self):
        """Threat 3: stored and reflected XSS. The raw payload must never reach the HTML."""
        for url in self.pages():
            response = self.client.get(url)
            self.assertIn(response.status_code, (200, 302, 404), url)
            html = response.content.decode()
            for payload in XSS[:3]:
                self.assertNotIn(payload, html, url)
            self.assertNotIn("<img src=x onerror", html, url)
            self.assertNotIn("<svg/onload", html, url)
            self.assertNotIn("{{7*7}}49", html, url)

    def test_template_syntax_typed_by_the_owner_is_not_evaluated(self):
        self.client.get(reverse("core:search") + "?q={{7*7}}")
        html = self.client.get(reverse("core:search") + "?q={{7*7}}").content.decode()
        self.assertNotIn(">49<", html)

    def test_sql_metacharacters_are_data_not_code(self):
        before = (get_user_model().objects.count(), Category.objects.count())
        for q in (
            "'; DROP TABLE members_member;--",
            "' OR '1'='1",
            "%' UNION SELECT 1--",
            "\\",
            "\x00",
            "%00",
        ):
            for url in (
                reverse("members:list") + "?q=",
                reverse("core:search") + "?q=",
                reverse("catalogue:edition_list") + "?q=",
                reverse("finance:payments") + "?q=",
                reverse("finance:payments") + "?receipt=",
            ):
                self.assertIn(
                    self.client.get(url, {}, QUERY_STRING=url.split("?")[1] + q).status_code,
                    (200,),
                    (url, q),
                )
        self.assertEqual((get_user_model().objects.count(), Category.objects.count()), before)
        self.assertTrue(members.Member.objects.filter(pk=self.member.pk).exists())

    def test_ids_in_the_path_cannot_reach_other_users_or_cause_errors(self):
        """Single owner: no other user data exists; absurd ids must give 404, not 500."""
        for url in (
            reverse("members:detail", args=[999999999999]),
            reverse("finance:receipt", args=[999999999999]),
            reverse("finance:payments_detail", args=[999999999999]),
        ):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        self.assertEqual(self.client.get("/members/99999999999999999999999/").status_code, 404)

    def test_download_names_are_fixed_and_not_taken_from_user_input(self):
        for url in (
            reverse("reports:report", args=["collections"]) + "?preset=year&format=csv",
            reverse("catalogue:import_template"),
        ):
            disposition = self.client.get(url)["Content-Disposition"]
            self.assertRegex(disposition, r'^attachment; filename="[A-Za-z0-9._-]+"$')


class ErrorPages(TestCase):
    def test_a_crash_shows_no_traceback_path_setting_or_secret(self):
        make_owner()
        client = Client(raise_request_exception=False)
        client.login(username="owner", password=PW)
        with mock.patch(
            "core.dashboard.search", side_effect=RuntimeError("SECRET-DB-PASSWORD /srv/app/x.py")
        ):
            response = client.get(reverse("core:search") + "?q=a")
        self.assertEqual(response.status_code, 500)
        body = response.content.decode()
        for leak in (
            "SECRET-DB-PASSWORD",
            "Traceback",
            "/srv/app",
            "RuntimeError",
            "SECRET_KEY",
            "DB_PASSWORD",
        ):
            self.assertNotIn(leak, body)

    def test_not_found_and_bad_requests_are_plain(self):
        for url in ("/nope/", "/members/abc/", "/static/../manage.py"):
            body = self.client.get(url).content.decode()
            self.assertNotIn("Traceback", body)
            self.assertNotIn("DEBUG", body)
            self.assertNotIn("URLconf", body)  # the debug 404 lists every route

    @override_settings(ALLOWED_HOSTS=["127.0.0.1", "localhost"])
    def test_a_foreign_host_header_is_refused(self):
        for host in ("evil.example", "127.0.0.1.evil.example", "localhost:8000@evil.example"):
            self.assertEqual(
                self.client.get("/accounts/login/", HTTP_HOST=host).status_code, 400, host
            )
        self.assertEqual(
            self.client.get(
                "/accounts/login/", HTTP_HOST="127.0.0.1", HTTP_X_FORWARDED_HOST="evil.example"
            ).status_code,
            200,
        )

    def test_the_forwarding_header_does_not_change_the_url_the_application_builds(self):
        make_owner()
        response = self.client.post(
            reverse("accounts:password_reset"),
            {"email": "x@example.com"},
            HTTP_X_FORWARDED_HOST="evil.example",
            HTTP_X_FORWARDED_PROTO="https",
        )
        self.assertNotIn("evil.example", response["Location"])
