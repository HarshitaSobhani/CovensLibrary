"""Phase 5b: screens, the post-write middleware, WhatsApp (manual) and dashboard integration."""

import smtplib
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from audit.models import AuditEvent
from circulation.tests.helpers import D, make_copies, make_owner, member_at, overdue_loan
from communications import messages
from communications import services as cs
from communications.models import MessageTemplate, Notification
from core import dashboard
from core.models import LibrarySetting
from members import services as members
from memberships.tests.helpers import PW

from .helpers import give_email, ist

S = Notification.Status


class ScreenTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.client.login(username="owner", password=PW)
        self.member = give_email(member_at(self.owner))

    def test_pages_render_and_filters_work(self):
        cs.run_cycle(ist(2026, 11, 11, 10))
        for status in ("all", "open", "problem", "accepted", "skipped", "bogus"):
            response = self.client.get(reverse("communications:list") + f"?status={status}")
            self.assertEqual(response.status_code, 200, status)
        note = Notification.objects.filter(status=S.SMTP_ACCEPTED).first()
        page = self.client.get(reverse("communications:detail", args=[note.pk])).content.decode()
        self.assertIn("SMTP accepted", page)
        self.assertIn("not proof of delivery", page)
        self.assertNotIn("Delivered", page)

    def test_bookkeeping_events_are_hidden_from_the_default_list(self):
        cs.consume_events(ist(2026, 11, 11, 10))
        default = self.client.get(reverse("communications:list")).content.decode()
        self.assertNotIn("No email defined for this event", default)

    def test_process_now_runs_a_cycle(self):
        response = self.client.post(reverse("communications:process"), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Notification.objects.filter(status=S.SMTP_ACCEPTED).exists())
        self.assertGreaterEqual(len(mail.outbox), 2)

    def test_get_is_refused_for_actions(self):
        for name in ("communications:process", "communications:test_email"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 405)

    def test_test_email_needs_an_address_and_reports_smtp_acceptance_honestly(self):
        page = self.client.post(reverse("communications:test_email"), follow=True).content.decode()
        self.assertIn("no email address", page)
        self.owner.email = "owner@example.com"
        self.owner.save()
        page = self.client.post(reverse("communications:test_email"), follow=True).content.decode()
        self.assertIn("does not prove delivery", page)
        self.assertEqual(mail.outbox[-1].to, ["owner@example.com"])
        with mock.patch.object(cs.EmailMessage, "send", side_effect=smtplib.SMTPException("down")):
            page = self.client.post(
                reverse("communications:test_email"), follow=True
            ).content.decode()
        self.assertIn("The test email failed", page)
        self.assertEqual(Notification.objects.filter(kind="none", source="schedule").count(), 0)


class MiddlewareTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.client.login(username="owner", password=PW)

    def test_a_successful_write_sends_the_immediate_notifications(self):
        from django.utils import timezone

        from memberships.tests.helpers import register

        member = give_email(register(self.owner, today=timezone.localdate()).member)
        copy = make_copies(self.owner, 1)[0]
        mail.outbox.clear()
        response = self.client.post(
            reverse("circulation:issue"),
            {"member_no": member.membership_no, "copy_code": copy.copy_code},
            follow=True,
        )
        self.assertContains(response, "Issued " + copy.copy_code)
        subjects = [m.subject for m in mail.outbox]
        self.assertTrue(any(x.startswith("Book issued") for x in subjects), subjects)
        self.assertTrue(any("receipt" in x.lower() for x in subjects), subjects)  # registration's
        self.assertEqual(len(subjects), len(set(subjects)))  # no duplicate emails
        self.client.post(reverse("core:settings"), {"action": "down", "section": "overdue"})
        self.assertEqual(len(mail.outbox), len(subjects))  # a later write sends nothing twice

    def test_get_requests_and_failed_posts_do_not_run_a_cycle(self):
        with mock.patch.object(cs, "run_cycle") as run:
            self.client.get(reverse("core:dashboard"))
            self.client.post(
                reverse("core:settings"), {"action": "save", "expiry_window_days": "0"}
            )
            self.assertEqual(
                run.call_count, 1
            )  # the redirect after the invalid save is a 302 < 400

    def test_a_failing_cycle_never_breaks_the_response(self):
        with mock.patch.object(cs, "run_cycle", side_effect=RuntimeError("boom")):
            response = self.client.post(
                reverse("core:settings"), {"action": "down", "section": "overdue"}
            )
        self.assertEqual(response.status_code, 302)

    @override_settings(NOTIFY_AFTER_WRITE=False)
    def test_it_can_be_switched_off(self):
        with mock.patch.object(cs, "run_cycle") as run:
            self.client.post(reverse("core:settings"), {"action": "down", "section": "overdue"})
        run.assert_not_called()


class WhatsAppTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.client.login(username="owner", password=PW)

    def test_a_prefilled_link_is_prepared_and_nothing_is_sent(self):
        member, copy, loan = overdue_loan(self.owner, name="Asha Rao", number="9876543210")
        mail.outbox.clear()
        before = Notification.objects.count()
        page = self.client.get(
            reverse("communications:whatsapp", args=[member.pk]) + "?template=due"
        )
        self.assertEqual(page.status_code, 200)
        link = page.context["link"]
        url = urlparse(link)
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "wa.me", "/919876543210"))
        text = parse_qs(url.query)["text"][0]
        self.assertIn("Asha Rao", text)
        self.assertIn("B-9876543210", text)
        self.assertIn("31 December 2026", text)
        self.assertEqual((len(mail.outbox), Notification.objects.count()), (0, before))
        self.assertIn("you review and send it yourself", page.content.decode())
        self.assertIn('rel="noopener noreferrer"', page.content.decode())

    def test_every_default_template_renders_with_whitelisted_placeholders_only(self):
        member = member_at(self.owner)
        for key in messages.DEFAULT_TEMPLATES:
            self.assertEqual(
                messages.unknown_placeholders(messages.DEFAULT_TEMPLATES[key]), [], key
            )
            self.assertEqual(
                self.client.get(
                    reverse("communications:whatsapp", args=[member.pk]) + f"?template={key}"
                ).status_code,
                200,
            )
        self.assertEqual(
            self.client.get(
                reverse("communications:whatsapp", args=[member.pk]) + "?template=zzz"
            ).status_code,
            200,
        )

    def test_placeholders_are_substituted_not_evaluated(self):
        text = messages.render_whatsapp(
            "{{name}} {{ name.__class__ }} {{secret}} {{title}}",
            {"name": "A", "title": "T", "secret": "S"},
        )
        self.assertEqual(text, "A {{ name.__class__ }} {{secret}} T")

    def test_anonymised_members_are_refused(self):
        member = member_at(self.owner)
        members.anonymise_member(member, actor=self.owner, reason="r", confirm=True)
        page = self.client.get(reverse("communications:whatsapp", args=[member.pk]))
        self.assertEqual(page.context["link"], "")
        self.assertIn("anonymised", page.context["refusal"])

    def test_templates_are_editable_validated_and_audited(self):
        url = reverse("communications:templates")
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url, {"key": "welcome", "body": "Hi {{name}} from {{library}}"})
        self.assertEqual(
            MessageTemplate.objects.get(key="welcome").body, "Hi {{name}} from {{library}}"
        )
        self.assertTrue(AuditEvent.objects.filter(action="whatsapp.template_edited").exists())
        for key, body in (
            ("welcome", "Hi {{password}}"),
            ("welcome", "  "),
            ("nope", "x"),
            ("welcome", "x" * 2001),
        ):
            self.client.post(url, {"key": key, "body": body})
        self.assertEqual(
            MessageTemplate.objects.get(key="welcome").body, "Hi {{name}} from {{library}}"
        )
        member = member_at(self.owner)
        page = self.client.get(
            reverse("communications:whatsapp", args=[member.pk]) + "?template=welcome"
        )
        self.assertIn(
            f"Hi {member.full_name} from {LibrarySetting.load().library_name}", page.context["text"]
        )

    def test_the_member_page_links_to_whatsapp_only_when_not_anonymised(self):
        member = member_at(self.owner)
        page = self.client.get(reverse("members:detail", args=[member.pk])).content.decode()
        self.assertIn(reverse("communications:whatsapp", args=[member.pk]), page)
        members.anonymise_member(member, actor=self.owner, reason="r", confirm=True)
        page = self.client.get(reverse("members:detail", args=[member.pk])).content.decode()
        self.assertNotIn(reverse("communications:whatsapp", args=[member.pk]), page)


class NoAutomaticWhatsAppTests(TestCase):
    def test_no_code_path_calls_a_whatsapp_api(self):
        import ast
        from pathlib import Path

        from django.conf import settings

        for path in Path(settings.BASE_DIR).rglob("*.py"):
            if any(p in path.parts for p in (".venv", "tests", "migrations")):
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    name = getattr(node, "module", None) or ""
                    names = [name] + [a.name for a in node.names]
                    self.assertFalse(
                        [
                            n
                            for n in names
                            if n
                            and any(w in n.lower() for w in ("twilio", "pywhatkit", "whatsapp"))
                        ],
                        path,
                    )
        sources = " ".join(
            p.read_text() for p in Path(settings.BASE_DIR, "communications").glob("*.py")
        )
        self.assertNotIn("requests.", sources)
        self.assertNotIn("urlopen", sources)


class DashboardIntegrationTests(TestCase):
    def test_failed_emails_appear_on_the_dashboard_with_the_error(self):
        owner = make_owner()
        give_email(member_at(owner))
        cs.consume_events(ist(2026, 11, 11))
        with mock.patch.object(
            cs.EmailMessage, "send", side_effect=smtplib.SMTPException("relay down")
        ):
            cs.send_due(ist(2026, 11, 11, 10))
        setting = LibrarySetting.load()
        data = {s["key"]: s["data"] for s in dashboard.build(setting, D(2026, 11, 12))["sections"]}
        self.assertTrue(data["failed_emails"]["available"])
        self.assertEqual(data["failed_emails"]["count"], 2)
        self.client.login(username="owner", password=PW)
        page = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertIn("relay down", page)
        # a successful retry removes it from the section
        cs.send_due(ist(2026, 11, 11, 10, 6))
        data = {s["key"]: s["data"] for s in dashboard.build(setting, D(2026, 11, 12))["sections"]}
        self.assertEqual(data["failed_emails"]["count"], 0)
