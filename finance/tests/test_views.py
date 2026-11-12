import uuid

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from finance import tokens
from finance.models import FinancialTransaction
from memberships.tests.helpers import PW, forfeit, make_owner, register


class CorrectionViewTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.client.login(username="owner", password=PW)
        self.today = timezone.localdate()
        self.member = register(self.owner, today=self.today).member

    def key(self):
        return str(uuid.uuid4())

    def test_refund_page_and_flow(self):
        url = reverse("finance:refund", args=[self.member.pk])
        page = self.client.get(url)
        self.assertContains(page, "partial refunds do not exist")
        bad = self.client.post(url, {"pay_cash": "100", "request_key": self.key()})
        self.assertContains(bad, "Nothing is activated or recorded")
        r = self.client.post(
            url,
            {"pay_cash": "200", "pay_upi": "300", "pay_upi_ref": "R9", "request_key": self.key()},
            follow=True,
        )
        self.assertContains(r, f"REF-{self.today.year}-000001")
        self.assertEqual(tokens.token_state(self.member)[1], tokens.REFUNDED)

    def test_refund_of_a_forfeited_token_shows_the_rule(self):
        forfeit(self.owner, self.member, today=self.today)
        r = self.client.post(
            reverse("finance:refund", args=[self.member.pk]),
            {"pay_cash": "500", "request_key": self.key()},
        )
        self.assertContains(r, "never refundable")

    def test_reverse_flow_keeps_the_original_and_hides_the_link_afterwards(self):
        original = forfeit(self.owner, self.member, today=self.today).transactions[0]
        detail = reverse("members:detail", args=[self.member.pk])
        self.assertContains(self.client.get(detail), reverse("finance:reverse", args=[original.pk]))
        url = reverse("finance:reverse", args=[original.pk])
        self.assertContains(
            self.client.post(url, {"reason": " ", "confirm": "on", "request_key": self.key()}),
            "required",
        )
        r = self.client.post(
            url, {"reason": "mistake", "confirm": "on", "request_key": self.key()}, follow=True
        )
        self.assertContains(r, "original record is unchanged")
        self.assertNotContains(r, url)
        self.assertEqual(FinancialTransaction.objects.filter(kind="reversal").count(), 1)

    def test_fee_reversal_is_refused_in_the_interface(self):
        fee = FinancialTransaction.objects.get(kind="subscription_payment")
        r = self.client.post(
            reverse("finance:reverse", args=[fee.pk]),
            {"reason": "oops", "confirm": "on", "pay_cash": "800", "request_key": self.key()},
        )
        self.assertContains(r, "never refunded or reversed")
        self.assertNotContains(
            self.client.get(reverse("members:detail", args=[self.member.pk])),
            reverse("finance:reverse", args=[fee.pk]),
        )

    def test_adjustment_flow_requires_approval_and_reason(self):
        url = reverse("finance:adjust", args=[self.member.pk])
        data = {
            "direction": "out",
            "amount": "500",
            "pay_cash": "500",
            "reason": "book found",
            "confirm_not_fee_refund": "on",
            "request_key": self.key(),
        }
        self.assertContains(self.client.post(url, data), "required")  # no approval ticked
        r = self.client.post(url, {**data, "approved": "on"}, follow=True)
        self.assertContains(r, f"ADJ-{self.today.year}-000001")
        self.assertTrue(AuditEvent.objects.filter(action="finance.adjustment").exists())
        again = self.client.post(url, {**data, "approved": "on"})  # same key: not repeated
        self.assertContains(again, "already submitted")
        self.assertEqual(FinancialTransaction.objects.filter(kind="adjustment").count(), 1)

    def test_login_is_required_for_every_correction_page(self):
        self.client.logout()
        for name, args in (
            ("finance:refund", [self.member.pk]),
            ("finance:adjust", [self.member.pk]),
            ("finance:reverse", [1]),
        ):
            r = self.client.get(reverse(name, args=args))
            self.assertEqual(r.status_code, 302)
            self.assertIn("/accounts/login/", r["Location"])
