"""Receipt pages and PDFs: owner-only, and the original payment reference is retained for an
anonymised member (decision R-59) while the old name is not shown."""

from unittest import mock

from django.test import TestCase
from django.urls import reverse
from reportlab import rl_config

from circulation.tests.helpers import D, Entry, make_owner
from members import services as members
from memberships.tests.helpers import PW, register

NAME = "Zainab Qureshi"
REFERENCE = "UPI-ZAINAB-77"


def uncompressed():
    """PDF text is only inspectable when stream compression is off."""
    return mock.patch.object(rl_config, "pageCompression", 0)


class ReceiptAccessTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member = register(
            self.owner,
            name=NAME,
            number="9811100071",
            today=D(2026, 11, 11),
            subscription_entries=[
                Entry("cash", 30000),
                Entry("upi", 50000, reference=REFERENCE),
            ],
        ).member
        self.document = self.member.receipts.get()
        self.page = reverse("finance:receipt", args=[self.document.pk])
        self.pdf = reverse("finance:receipt_pdf", args=[self.document.pk])

    def test_the_authenticated_owner_can_open_the_page_and_the_pdf(self):
        self.client.login(username="owner", password=PW)
        page = self.client.get(self.page)
        self.assertEqual(page.status_code, 200)
        self.assertIn(REFERENCE, page.content.decode())
        pdf = self.client.get(self.pdf)
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))

    def test_without_login_both_routes_redirect_to_the_login_page(self):
        for url in (self.page, self.pdf):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertTrue(response["Location"].startswith("/accounts/login/"), url)
            self.assertIn("next=", response["Location"])
        self.assertEqual(self.client.post(self.page).status_code, 302)

    def test_an_unknown_receipt_is_a_404_for_the_owner_and_a_login_redirect_otherwise(self):
        missing = reverse("finance:receipt", args=[999999])
        self.assertEqual(self.client.get(missing).status_code, 302)
        self.client.login(username="owner", password=PW)
        self.assertEqual(self.client.get(missing).status_code, 404)
        self.assertEqual(
            self.client.get(reverse("finance:receipt_pdf", args=[999999])).status_code, 404
        )

    def test_after_anonymisation_the_receipt_keeps_the_reference_but_not_the_name(self):
        self.client.login(username="owner", password=PW)
        before = self.client.get(self.page).content.decode()
        self.assertIn(NAME, before)  # the live name is shown on a live member's receipt
        members.anonymise_member(self.member, actor=self.owner, reason="request", confirm=True)
        page = self.client.get(self.page)
        self.assertEqual(page.status_code, 200)
        text = page.content.decode()
        self.assertIn(REFERENCE, text)  # the immutable receipt keeps what was recorded
        self.assertNotIn("Zainab", text)
        self.assertNotIn("Qureshi", text)
        self.assertNotIn("9811100071", text)
        with uncompressed():
            pdf = self.client.get(self.pdf)
        self.assertEqual(pdf.status_code, 200)
        self.assertIn(REFERENCE.encode(), pdf.content)
        self.assertNotIn(b"Zainab", pdf.content)
        self.assertNotIn(b"Qureshi", pdf.content)

    def test_the_anonymised_receipt_is_still_owner_only(self):
        members.anonymise_member(self.member, actor=self.owner, reason="request", confirm=True)
        for url in (self.page, self.pdf):
            self.assertEqual(self.client.get(url).status_code, 302, url)
