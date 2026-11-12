import uuid
from datetime import date

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from finance.models import FinancialTransaction, ReceiptDocument
from members.models import Member
from memberships.models import MemberSubscription

from .helpers import PW, forfeit, make_owner, register


class ViewTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.client.login(username="owner", password=PW)
        self.today = timezone.localdate()

    def reg_data(self, **kw):
        data = {
            "full_name": "Asha Rao",
            "region": "IN",
            "national_number": "9876543210",
            "email": "",
            "joined_on": self.today.isoformat(),
            "notes": "",
            "plan": "6m",
            "activation_date": self.today.isoformat(),
            "sub_cash": "300.00",
            "sub_upi": "500.00",
            "sub_upi_ref": "UPI-77",
            "tok_cash": "500",
            "tok_upi": "",
            "tok_upi_ref": "",
            "payment_date": "",
            "backdate_reason": "",
            "request_key": str(uuid.uuid4()),
        }
        data.update(kw)
        return data

    def test_registration_with_split_payment_shows_the_combined_printout(self):
        r = self.client.post(reverse("memberships:register"), self.reg_data(), follow=True)
        self.assertEqual(r.status_code, 200)
        doc = ReceiptDocument.objects.get()
        self.assertContains(r, f"SUB-{self.today.year}-000001")
        self.assertContains(r, f"TOK-{self.today.year}-000001")
        self.assertContains(r, "₹800.00")
        self.assertContains(r, "UPI-77")
        self.assertEqual(doc.kind, "registration")
        self.assertEqual(Member.objects.count(), 1)

    def test_partial_payment_shows_an_error_keeps_the_data_and_records_nothing(self):
        r = self.client.post(reverse("memberships:register"), self.reg_data(sub_upi="499.99"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Nothing is activated or recorded until the full amount is paid")
        self.assertContains(r, 'value="Asha Rao"')
        self.assertEqual(Member.objects.count(), 0)
        self.assertEqual(FinancialTransaction.objects.count(), 0)

    def test_amounts_with_more_than_two_decimals_are_rejected_by_the_form(self):
        r = self.client.post(reverse("memberships:register"), self.reg_data(sub_cash="300.005"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Member.objects.count(), 0)

    def test_double_posting_the_same_form_registers_once(self):
        data = self.reg_data()
        self.client.post(reverse("memberships:register"), data)
        again = self.client.post(reverse("memberships:register"), data)
        self.assertEqual(again.status_code, 200)  # re-rendered with an error, not a second member
        self.assertEqual(Member.objects.count(), 1)
        self.assertEqual(MemberSubscription.objects.count(), 1)

    def test_member_page_shows_subscription_dates_token_and_records(self):
        member = register(self.owner, today=self.today).member
        r = self.client.get(reverse("members:detail", args=[member.pk]))
        self.assertContains(r, "Held (refundable)")
        self.assertContains(r, "6-month subscription")
        self.assertContains(r, "Last valid day")
        self.assertContains(r, f"SUB-{self.today.year}-000001")

    def test_forfeited_member_page_explains_the_block_and_hides_renewal(self):
        member = register(self.owner, today=self.today).member
        forfeit(self.owner, member, today=self.today)
        r = self.client.get(reverse("members:detail", args=[member.pk]))
        self.assertContains(r, "Forfeited")
        self.assertContains(r, "Collect token")
        self.assertNotContains(r, "Renew / add subscription")
        blocked = self.client.get(reverse("memberships:renew", args=[member.pk]))
        self.assertContains(blocked, "token was forfeited")

    def test_renewal_and_fresh_token_flows(self):
        member = register(self.owner, today=self.today).member
        forfeit(self.owner, member, today=self.today)
        r = self.client.post(
            reverse("memberships:fresh_token", args=[member.pk]),
            {"tok_cash": "500", "request_key": str(uuid.uuid4())},
            follow=True,
        )
        self.assertContains(r, "new token cycle")
        r = self.client.post(
            reverse("memberships:renew", args=[member.pk]),
            {"plan": "12m", "sub_cash": "1500", "request_key": str(uuid.uuid4())},
            follow=True,
        )
        self.assertContains(r, "₹1,500.00")
        self.assertEqual(member.subscriptions.count(), 2)

    def test_cancel_requires_the_acknowledgement_and_gives_no_refund(self):
        member = register(self.owner, today=self.today).member
        sub = member.subscriptions.get()
        url = reverse("memberships:cancel", args=[sub.pk])
        self.client.post(url, {"reason": "left town"})
        sub.refresh_from_db()
        self.assertIsNone(sub.cancelled_at)  # missing acknowledgement
        r = self.client.post(url, {"reason": "left town", "confirm": "on"}, follow=True)
        self.assertContains(r, "No refund or credit")
        sub.refresh_from_db()
        self.assertIsNotNone(sub.cancelled_at)
        self.assertEqual(FinancialTransaction.objects.count(), 2)

    def test_receipt_html_and_pdf(self):
        result = register(self.owner, today=self.today)
        html = self.client.get(reverse("finance:receipt", args=[result.document.pk]))
        self.assertContains(html, result.member.full_name)
        pdf = self.client.get(reverse("finance:receipt_pdf", args=[result.document.pk]))
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertEqual(self.client.get(reverse("finance:receipt", args=[99999])).status_code, 404)

    def test_everything_requires_login(self):
        self.client.logout()
        member = register(self.owner, today=date(2026, 11, 11)).member
        for name, args in (
            ("memberships:register", []),
            ("memberships:renew", [member.pk]),
            ("memberships:fresh_token", [member.pk]),
            ("finance:receipt", [1]),
            ("finance:receipt_pdf", [1]),
        ):
            r = self.client.get(reverse(name, args=args))
            self.assertEqual(r.status_code, 302, name)
            self.assertIn("/accounts/login/", r["Location"])
