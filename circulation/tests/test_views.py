from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from circulation.models import Loan

from .helpers import PW, make_copies, make_owner, member_at


class LoanViewTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.client.login(username="owner", password=PW)
        self.today = timezone.localdate()
        self.member = member_at(self.owner, today=self.today)
        self.copy = make_copies(self.owner, 2)[0]

    def issue(self, **extra):
        data = {"member_no": self.member.membership_no, "copy_code": self.copy.copy_code, **extra}
        return self.client.post(reverse("circulation:issue"), data, follow=True)

    def test_issue_by_member_id_and_copy_id_or_barcode(self):
        r = self.issue()
        self.assertContains(r, "Issued " + self.copy.copy_code)
        loan = Loan.objects.get()
        self.assertEqual(loan.due_on, self.today + timedelta(days=30))
        self.assertContains(r, "forfeited automatically on")

    def test_unknown_member_or_copy_shows_a_field_error(self):
        r = self.client.post(
            reverse("circulation:issue"), {"member_no": "COV-M-999999", "copy_code": "nope"}
        )
        self.assertContains(r, "No member has this ID.")
        self.assertContains(r, "No copy has this ID or barcode.")

    def test_rule_violations_are_shown_and_nothing_is_recorded(self):
        self.issue()
        r = self.client.post(
            reverse("circulation:issue"),
            {"member_no": self.member.membership_no, "copy_code": self.copy.barcode},
        )
        self.assertContains(r, "text-danger")
        self.assertEqual(Loan.objects.count(), 1)

    def test_return_flow_and_lists(self):
        self.issue()
        loan = Loan.objects.get()
        listing = self.client.get(reverse("circulation:list"))
        self.assertContains(listing, self.copy.copy_code)
        self.assertContains(
            self.client.get(reverse("circulation:list") + "?show=overdue"), "No loans."
        )
        r = self.client.post(
            reverse("circulation:return", args=[loan.pk]), {"condition": "good"}, follow=True
        )
        self.assertContains(r, "Book returned.")
        loan.refresh_from_db()
        self.assertEqual(loan.status, "returned")
        self.assertContains(self.client.get(reverse("circulation:list") + "?show=all"), "Returned")

    def test_everything_requires_login(self):
        self.client.logout()
        for name, args in (
            ("circulation:list", []),
            ("circulation:issue", []),
            ("circulation:detail", [1]),
            ("circulation:return", [1]),
        ):
            r = self.client.get(reverse(name, args=args))
            self.assertEqual(r.status_code, 302, name)
            self.assertIn("/accounts/login/", r["Location"])


class OutcomeAndGraceViewTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.client.login(username="owner", password=PW)
        self.today = timezone.localdate()
        self.member = member_at(self.owner, today=self.today)
        self.copy = make_copies(self.owner, 1)[0]
        self.client.post(
            reverse("circulation:issue"),
            {"member_no": self.member.membership_no, "copy_code": self.copy.copy_code},
        )
        self.loan = Loan.objects.get()

    def test_renew_from_the_screen(self):
        r = self.client.post(
            reverse("circulation:renew", args=[self.loan.pk]), {"notes": "more time"}, follow=True
        )
        self.assertContains(r, "Renewed; now due")
        self.assertEqual(self.loan.renewals.count(), 1)
        self.assertContains(r, "more time")

    def test_poor_return_needs_the_confirmation_and_reason_on_screen(self):
        url = reverse("circulation:return", args=[self.loan.pk])
        r = self.client.post(url, {"condition": "poor"})
        self.assertContains(r, "Confirm the outcome")
        r = self.client.post(url, {"condition": "poor", "owner_confirmed": "on"})
        self.assertContains(r, "reason for this outcome is required")
        r = self.client.post(
            url, {"condition": "poor", "owner_confirmed": "on", "reason": "torn pages"}, follow=True
        )
        self.assertContains(r, "Book returned.")
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.return_condition, "poor")
        self.assertEqual(self.loan.copy.condition, "poor")
        self.assertContains(r, "Forfeited")

    def test_mark_lost_from_the_screen(self):
        url = reverse("circulation:lost", args=[self.loan.pk])
        self.assertContains(self.client.post(url, {"notes": ""}), "Confirm the outcome")
        r = self.client.post(url, {"owner_confirmed": "on", "reason": "stolen"}, follow=True)
        self.assertContains(r, "Marked as lost")
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "lost")

    def test_grace_page_decisions_and_history(self):
        page = self.client.get(reverse("circulation:grace"))
        self.assertContains(page, "7 days")
        self.assertContains(page, "No changes yet.")
        r = self.client.post(reverse("circulation:grace"), {"grace_days": "7"})
        self.assertContains(r, "already that many days")
        r = self.client.post(reverse("circulation:grace"), {"grace_days": "3"}, follow=True)
        self.assertContains(r, "Grace period is now 3 days.")
        self.assertContains(self.client.get(reverse("circulation:grace")), "7 &rarr; 3")
        self.assertContains(
            self.client.get(reverse("circulation:decisions")), "No decisions waiting."
        )

    def test_decision_screens_confirm_and_decline(self):
        from circulation import grace as grace_service
        from circulation import services as loan_services

        # a loan issued 35 days ago (backdated, with reason) is now 5 days overdue
        past = self.today - timedelta(days=35)
        member = member_at(
            self.owner,
            "Dee",
            "9876543299",
            today=self.today,
            activation_date=self.today - timedelta(days=60),
            backdate_reason="joined earlier",
            backdate_confirmed=True,
        )
        loan = loan_services.issue_loan(
            actor=self.owner,
            member=member,
            copy=make_copies(self.owner, 1, "Old")[0],
            issued_on=past,
            backdate_reason="entered late",
            backdate_confirmed=True,
        )
        self.loan, self.copy = loan, loan.copy
        grace_service.change_grace_days(actor=self.owner, new_days=3)
        page = self.client.get(reverse("circulation:decisions"))
        self.assertContains(page, self.copy.copy_code)
        pending = self.loan.pending_forfeitures.get()
        resolve = lambda action, reason: self.client.post(  # noqa: E731
            reverse("circulation:decision_resolve", args=[pending.pk, action]),
            {"reason": reason},
            follow=True,
        )
        self.assertContains(resolve("decline", ""), "A reason is required.")
        self.assertContains(resolve("decline", "member was ill"), "You will not be asked again")
        self.assertContains(resolve("confirm", "again"), "already made")
        self.assertContains(self.client.get(reverse("circulation:decisions")), "Declined")

    def test_new_pages_require_login(self):
        self.client.logout()
        for name, args in (
            ("circulation:renew", [self.loan.pk]),
            ("circulation:lost", [self.loan.pk]),
            ("circulation:grace", []),
            ("circulation:decisions", []),
            ("circulation:decision_resolve", [1, "confirm"]),
        ):
            r = self.client.get(reverse(name, args=args))
            self.assertEqual(r.status_code, 302, name)
            self.assertIn("/accounts/login/", r["Location"])
