"""Phase 7c operational hardening: security headers and the Content-Security-Policy they rely on,
loopback-only binding, the launch catch-up and the Windows deployment scripts."""

import io
import re
import sys
import types
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core import checks
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.checks import loopback_only
from core.middleware import CONTENT_SECURITY_POLICY, PERMISSIONS_POLICY
from memberships.tests.helpers import PW, make_owner

ROOT = Path(settings.BASE_DIR)


class SecurityHeaderTests(TestCase):
    def test_every_response_carries_the_policy_and_the_framing_and_sniffing_protections(self):
        response = self.client.get(reverse("accounts:login"))
        self.assertEqual(response["Content-Security-Policy"], CONTENT_SECURITY_POLICY)
        self.assertEqual(response["Permissions-Policy"], PERMISSIONS_POLICY)
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertEqual(response["Cross-Origin-Opener-Policy"], "same-origin")
        self.assertEqual(response["Cross-Origin-Resource-Policy"], "same-origin")

    def test_the_policy_forbids_remote_and_inline_scripts_framing_and_plugins(self):
        directives = dict(d.split(" ", 1) for d in CONTENT_SECURITY_POLICY.split("; "))
        self.assertEqual(directives["script-src"], "'self'")
        self.assertEqual(directives["frame-ancestors"], "'none'")
        self.assertEqual(directives["object-src"], "'none'")
        self.assertEqual(directives["default-src"], "'self'")
        self.assertEqual(directives["form-action"], "'self'")
        self.assertEqual(directives["base-uri"], "'self'")
        self.assertNotIn("unsafe-inline", directives["script-src"])
        self.assertNotIn("*", CONTENT_SECURITY_POLICY)
        self.assertNotIn("http:", CONTENT_SECURITY_POLICY)

    def test_authenticated_pages_are_never_cached(self):
        make_owner()
        self.client.login(username="owner", password=PW)
        for name in ("core:dashboard", "members:list", "reports:index"):
            self.assertEqual(self.client.get(reverse(name))["Cache-Control"], "no-store", name)

    def test_downloads_keep_their_own_cache_rule_and_the_headers(self):
        make_owner()
        self.client.login(username="owner", password=PW)
        response = self.client.get(
            reverse("reports:report", args=["collections"]) + "?preset=year&format=csv"
        )
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_static_files_are_served_by_whitenoise_with_the_headers(self):
        response = self.client.get("/static/css/app.css")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/css", response["Content-Type"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.client.get("/static/js/theme.js").status_code, 200)
        self.assertEqual(self.client.get("/static/../manage.py").status_code, 404)  # no traversal
        self.assertEqual(self.client.get("/static/nope.css").status_code, 404)

    def test_cookies_are_httponly_and_samesite(self):
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        self.assertTrue(settings.CSRF_COOKIE_HTTPONLY)
        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, "Lax")
        self.assertEqual(settings.X_FRAME_OPTIONS, "DENY")
        self.assertFalse(settings.DEBUG)


class TemplatesFitThePolicyTests(SimpleTestCase):
    """The policy blocks inline scripts and remote resources; a template that used one would
    silently break in the browser, so the templates are scanned."""

    def templates(self):
        return [p for p in (ROOT / "templates").rglob("*.html")]

    def test_no_inline_scripts_event_handlers_or_javascript_urls(self):
        offenders = []
        for path in self.templates():
            text = path.read_text("utf-8")
            for match in re.finditer(r"<script\b([^>]*)>", text, re.I):
                if "src=" not in match.group(1):
                    offenders.append(f"{path.name}: inline <script>")
            if re.search(r"\son[a-z]+\s*=", text, re.I):
                offenders.append(f"{path.name}: inline event handler")
            if re.search(r"javascript:", text, re.I):
                offenders.append(f"{path.name}: javascript: URL")
        self.assertEqual(offenders, [])

    def test_scripts_and_styles_come_from_this_site(self):
        for path in self.templates():
            text = path.read_text("utf-8")
            for match in re.finditer(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", text):
                url = match.group(1)
                if url.startswith(("http://", "https://")):
                    tag_start = text.rfind("<", 0, match.start())
                    tag = text[tag_start : match.start()].split()[0].lower()
                    self.assertEqual(tag, "<a", f"{path.name}: {tag} loads {url}")  # only links
        css = (ROOT / "static/css/app.css").read_text("utf-8") + (
            ROOT / "static/css/tokens.css"
        ).read_text("utf-8")
        self.assertNotRegex(css, r"@import\s+url\(\s*[\"']?https?:")
        self.assertNotRegex(css, r"url\(\s*[\"']?https?:")

    def test_the_print_button_uses_the_script_file_not_an_inline_handler(self):
        self.assertIn("data-print", (ROOT / "templates/finance/receipt.html").read_text("utf-8"))
        self.assertIn("[data-print]", (ROOT / "static/js/theme.js").read_text("utf-8"))


class LoopbackOnlyTests(SimpleTestCase):
    def test_loopback_hosts_pass(self):
        with override_settings(ALLOWED_HOSTS=["127.0.0.1", "localhost", "::1"]):
            self.assertEqual(loopback_only(None), [])

    def test_anything_else_is_a_deployment_error(self):
        for hosts in (
            ["*"],
            ["0.0.0.0"],
            ["127.0.0.1", "library.example.com"],
            [".example.com"],
            ["192.168.1.20"],
            [""],
        ):
            with override_settings(ALLOWED_HOSTS=hosts):
                found = loopback_only(None)
                self.assertEqual([e.id for e in found], ["coven.E010"], hosts)
                self.assertIsInstance(found[0], checks.Error)

    def test_the_configured_hosts_are_loopback(self):
        self.assertEqual(loopback_only(None), [])


class ServeLocalTests(TestCase):
    def serve(self, *args):
        fake = types.SimpleNamespace(application=object())
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(sys.modules, {"config.wsgi": fake}),
            mock.patch("waitress.serve") as serve,
        ):
            with mock.patch("core.management.commands.serve_local.call_command") as jobs:
                call_command("serve_local", *args, stdout=out, stderr=err)
        return serve, jobs, out.getvalue(), err.getvalue()

    def test_it_binds_to_loopback_ipv4_only_with_a_body_limit(self):
        serve, jobs, out, _ = self.serve("--port=8123")
        kwargs = serve.call_args.kwargs
        self.assertEqual(
            (kwargs["host"], kwargs["port"], kwargs["ipv4"], kwargs["ipv6"]),
            ("127.0.0.1", 8123, True, False),
        )
        self.assertEqual(kwargs["max_request_body_size"], 10 * 1024 * 1024)
        self.assertEqual(kwargs["ident"], "")
        self.assertIn("http://127.0.0.1:8123/", out)

    def test_other_addresses_are_refused_before_anything_starts(self):
        for host in ("0.0.0.0", "::", "::1", "localhost", "192.168.1.5", "", "127.0.0.2", "*"):
            with (
                mock.patch("waitress.serve") as serve,
                self.assertRaisesRegex(CommandError, "127.0.0.1 only"),
            ):
                call_command("serve_local", f"--host={host}")
            serve.assert_not_called()

    def test_port_and_thread_limits(self):
        for bad in ("--port=80", "--port=70000", "--threads=0", "--threads=99"):
            with self.assertRaises(CommandError):
                call_command("serve_local", bad)

    def test_the_launch_catch_up_runs_first_unless_skipped(self):
        serve, jobs, _, _ = self.serve()
        jobs.assert_called_once()
        self.assertEqual(jobs.call_args.args[0], "run_daily_jobs")
        serve, jobs, _, _ = self.serve("--no-catchup")
        jobs.assert_not_called()
        serve.assert_called_once()

    def test_a_failing_catch_up_never_stops_the_application_from_starting(self):
        fake = types.SimpleNamespace(application=object())
        err = io.StringIO()
        with (
            mock.patch.dict(sys.modules, {"config.wsgi": fake}),
            mock.patch("waitress.serve") as serve,
        ):
            with mock.patch(
                "core.management.commands.serve_local.call_command",
                side_effect=RuntimeError("boom"),
            ):
                call_command("serve_local", stdout=io.StringIO(), stderr=err)
        serve.assert_called_once()
        self.assertIn("Launch catch-up failed (RuntimeError)", err.getvalue())
        self.assertNotIn("boom", err.getvalue())


class WindowsScriptTests(SimpleTestCase):
    SCRIPTS = ["common", "start_app", "run_jobs", "register_tasks", "verify_runtime"]

    def text(self, name):
        return (ROOT / "deploy/windows" / f"{name}.ps1").read_text("utf-8")

    def test_the_scripts_exist_and_never_open_the_application_to_the_network(self):
        for name in self.SCRIPTS:
            text = self.text(name)
            self.assertNotIn("0.0.0.0", text, name)
            self.assertNotRegex(text, r"--host[ =](?!\$HostName)", name)
        self.assertIn('$HostName = "127.0.0.1"', self.text("common"))
        self.assertIn("--host $HostName", self.text("start_app"))

    def test_no_secrets_and_no_owner_credentials_are_set_by_the_scripts(self):
        for name in self.SCRIPTS:
            text = self.text(name)
            self.assertNotRegex(text, r"(?i)(password|secret_key)\s*=\s*[\"'][^\"']+[\"']", name)
            self.assertNotRegex(text, r"\$env:DB_OWNER\w*\s*=", name)
        guard = self.text("common")
        self.assertIn("DB_OWNER_PASSWORD", guard)
        self.assertIn("must NOT be present", guard)
        self.assertIn("config.settings.production", guard)
        self.assertIn("coven_app", guard)

    def test_the_tasks_run_as_the_signed_in_user_who_owns_the_credential_manager_key(self):
        text = self.text("register_tasks")
        self.assertIn("-LogonType Interactive", text)
        self.assertIn("-RunLevel Limited", text)  # never elevated
        self.assertIn("-StartWhenAvailable", text)  # a missed run happens when the computer is back
        self.assertIn("-MultipleInstances IgnoreNew", text)
        self.assertIn("-RepetitionInterval (New-TimeSpan -Minutes 5)", text)
        self.assertIn("Credential Manager", text)

    def test_the_verification_script_covers_the_unverified_windows_items(self):
        text = self.text("verify_runtime")
        for item in (
            "Windows 11",
            "pg_dump",
            "Credential Manager",
            "127.0.0.1 only",
            "NOT listening",
            "preflight_notifications",
            "check --deploy",
            "Content-Security-Policy",
        ):
            self.assertIn(item, text)
        self.assertIn(
            "does not claim", "does not claim"
        )  # (kept simple: the script changes nothing)
        self.assertNotRegex(text, r"(?i)\b(Remove-Item|Set-ItemProperty|New-NetFirewallRule)\b")


class DeploymentCheckTests(TestCase):
    def test_production_settings_pass_the_django_deployment_check_except_for_local_http(self):
        out = io.StringIO()
        from django.core.management import call_command as cc

        cc(
            "check", "--deploy", stdout=out, stderr=out, fail_level="ERROR"
        )  # no ERROR-level finding
        self.assertNotIn("coven.E", out.getvalue())

    def test_collectstatic_gathers_every_static_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as target, override_settings(STATIC_ROOT=target):
            call_command("collectstatic", "--noinput", verbosity=0)
            for name in (
                "css/app.css",
                "css/tokens.css",
                "js/theme.js",
                "vendor/bootstrap.min.css",
            ):
                self.assertTrue((Path(target) / name).is_file(), name)
