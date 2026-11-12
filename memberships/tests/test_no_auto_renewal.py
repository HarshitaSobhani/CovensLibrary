"""Regression: there is NO automatic subscription renewal (PRD 6.5, decision register).

A subscription is only ever created by an explicit owner action that carries a payment:
``register_member`` or ``renew_subscription``. The tests prove it three ways: by reading the
source (what can create a subscription, what could run unattended), by exercising time passing and
every management command, and by checking that renewal cannot happen without an actor and payment.
"""

import ast
import re
from datetime import date, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management import call_command, get_commands
from django.test import TestCase
from django.urls import reverse

from core.errors import RuleViolation
from finance.models import FinancialTransaction
from memberships import policy
from memberships import services as ms
from memberships.models import MemberSubscription

from .helpers import PW, TODAY, make_owner, register

ROOT = Path(settings.BASE_DIR)
APPS = (
    "accounts",
    "audit",
    "catalogue",
    "circulation",
    "core",
    "finance",
    "members",
    "memberships",
    "config",
)


def source_files():
    for app in APPS:
        for path in (ROOT / app).rglob("*.py"):
            parts = path.parts
            if "tests" in parts or "migrations" in parts or path.name.startswith("test_"):
                continue
            yield path


def calls_by_name(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if called == name:
                yield node


class SourceLevelTests(TestCase):
    def test_a_subscription_can_only_be_created_in_one_place(self):
        creators = []
        for path in source_files():
            text = path.read_text()
            if "MemberSubscription" in text and re.search(
                r"MemberSubscription\.objects\.(create|bulk_create|get_or_create|update_or_create)",
                text,
            ):
                creators.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(creators, ["memberships/services.py"])

    def test_that_creator_is_only_reached_from_register_and_renew(self):
        tree = ast.parse((ROOT / "memberships/services.py").read_text())
        callers = set()
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            if any(True for _ in calls_by_name(fn, "_create_subscription")):
                callers.add(fn.name)
        self.assertEqual(callers, {"register_member", "renew_subscription"})
        for path in source_files():  # and nothing outside services calls either entry point
            if path.name == "services.py" and path.parent.name == "memberships":
                continue
            tree = ast.parse(path.read_text())
            for name in ("_create_subscription", "register_member", "renew_subscription"):
                for call in calls_by_name(tree, name):
                    self.assertIn(
                        path.relative_to(ROOT).as_posix(),
                        {"memberships/views.py"},  # the owner's forms, behind login
                        f"{name} called from {path} line {call.lineno}",
                    )

    def test_no_background_job_scheduler_or_signal_receiver_touches_subscriptions(self):
        banned = {
            "celery",
            "apscheduler",
            "django_q",
            "huey",
            "dramatiq",
            "schedule",
            "crontab",
            "rq",
        }
        for path in source_files():
            tree = ast.parse(path.read_text())
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported |= {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertEqual(imported & banned, set(), f"scheduler import in {path}")
        for app in ("memberships", "finance"):
            for path in (ROOT / app).rglob("*.py"):
                if "tests" in path.parts or "migrations" in path.parts:
                    continue
                text = path.read_text()
                self.assertNotIn("post_save", text, path)
                self.assertNotIn("@receiver", text, path)
        reqs = " ".join(p.read_text().lower() for p in (ROOT / "requirements").glob("*.txt"))
        for lib in ("celery", "apscheduler", "django-q", "huey", "dramatiq"):
            self.assertNotIn(lib, reqs, lib)

    def test_no_management_command_exists_for_memberships_or_finance(self):
        commands = get_commands()
        offenders = [name for name, app in commands.items() if app in ("memberships", "finance")]
        self.assertEqual(offenders, [])
        self.assertFalse((ROOT / "memberships" / "management").exists())
        self.assertFalse((ROOT / "finance" / "management").exists())

    def test_no_url_renews_by_get_and_none_is_unauthenticated(self):
        for name in ("memberships:renew", "memberships:register", "memberships:fresh_token"):
            url = reverse(name, args=[1]) if name != "memberships:register" else reverse(name)
            self.assertEqual(self.client.get(url).status_code, 302)  # login required
            self.assertEqual(self.client.post(url, {}).status_code, 302)
            self.assertEqual(MemberSubscription.objects.count(), 0)


class BehaviourTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member = register(self.owner).member
        self.client.login(username="owner", password=PW)

    def snapshot(self):
        return (
            list(MemberSubscription.objects.values_list("id", "starts_on", "expires_on")),
            FinancialTransaction.objects.count(),
        )

    def test_time_passing_never_creates_a_subscription_or_payment(self):
        before = self.snapshot()
        sub = self.member.subscriptions.get()
        for day in (
            sub.expires_on - timedelta(days=1),
            sub.expires_on,
            sub.expires_on + timedelta(days=400),
        ):
            self.assertIn(policy.state(sub.period, day), (policy.ACTIVE, policy.EXPIRED))
            ms.current_subscription(self.member, day)
            ms.scheduled_subscription(self.member, day)
            ms.renewal_blockers(self.member, day)
            ms.borrowing_blockers(self.member, day)
        self.assertEqual(self.snapshot(), before)

    def test_viewing_pages_after_expiry_creates_nothing(self):
        before = self.snapshot()
        for name, args in (
            ("members:detail", [self.member.pk]),
            ("members:list", []),
            ("core:dashboard", []),
            ("memberships:renew", [self.member.pk]),
        ):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)
        self.assertEqual(self.snapshot(), before)

    def test_running_every_available_command_creates_nothing(self):
        before = self.snapshot()
        for command in ("check", "check_db_security"):
            call_command(command, verbosity=0)
        self.assertEqual(self.snapshot(), before)

    def test_renewal_needs_a_payment_and_never_defaults_to_one(self):
        before = self.snapshot()
        for entries in ([], None):
            with self.assertRaises((RuleViolation, TypeError)):
                ms.renew_subscription(
                    actor=self.owner,
                    member=self.member,
                    plan_code="6m",
                    entries=entries,
                    today=date(2027, 1, 1),
                )
        with self.assertRaises(TypeError):  # the actor and the payment are mandatory arguments
            ms.renew_subscription(member=self.member, plan_code="6m", today=date(2027, 1, 1))
        self.assertEqual(self.snapshot(), before)

    def test_the_policy_has_no_renewal_default_and_a_lapsed_member_stays_lapsed(self):
        sub = self.member.subscriptions.get()
        day = sub.expires_on + timedelta(days=30)
        self.assertIsNone(ms.current_subscription(self.member, day))
        self.assertEqual(self.member.subscriptions.count(), 1)
        with self.assertRaises(RuleViolation) as ctx:  # even an owner renewal needs a chosen date
            ms.renew_subscription(
                actor=self.owner, member=self.member, plan_code="6m", entries=[], today=day
            )
        self.assertIn(ctx.exception.code, {"activation_date_required", "payment_required"})
        self.assertGreater(day, TODAY)
