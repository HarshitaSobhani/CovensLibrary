"""Payments & Tokens workspace: read-only monitoring of the immutable ledger. Figures must agree
with the existing report services, tokens must never count as subscription revenue, and nothing on
these pages can change a record."""

from datetime import date
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from audit.models import AuditEvent
from finance import corrections as fc
from finance import reporting
from finance import workspace as ws
from finance.models import (
    FinancialTransaction,
    PaymentEntry,
    ReceiptDocument,
    TokenLedgerEntry,
)
from finance.services import Entry
from members import services as members
from memberships.tests.helpers import PW, forfeit, make_owner, register, renew
from reports import queries

K = FinancialTransaction.Kind
D = date


class Base(TestCase):
    """Three members: A (token refunded), B (token forfeited), C (token held)."""

    def setUp(self):
        self.owner = make_owner()
        self.a = register(self.owner, "6m", "Asha Rao", "9876543210", today=D(2026, 11, 11)).member
        self.b = register(
            self.owner,
            "12m",
            "Ravi Menon",
            "9123456780",
            today=D(2026, 11, 12),
            token_entries=[Entry("cash", 50000)],
        ).member
        self.c = register(
            self.owner, "6m", "Chitra Iyer", "9988776655", today=D(2026, 11, 13)
        ).member
        forfeit(self.owner, self.b, today=D(2026, 12, 1), trigger="lost")
        self.refund = fc.refund_token(
            actor=self.owner, member=self.a, entries=[Entry("cash", 50000)], today=D(2026, 12, 3)
        ).transactions[0]
        self.adjust_out = fc.post_adjustment(
            actor=self.owner,
            member=self.a,
            direction="out",
            amount_paise=10000,
            reason="duplicate token entry",
            owner_approved=True,
            confirm_not_fee_refund=True,
            entries=[Entry("cash", 10000)],
            today=D(2026, 12, 5),
        ).transactions[0]
        self.note = fc.post_adjustment(
            actor=self.owner,
            member=self.a,
            direction="none",
            amount_paise=100,
            reason="accounting note",
            owner_approved=True,
            today=D(2026, 12, 6),
        ).transactions[0]
        fc.reverse_transaction(
            actor=self.owner,
            transaction_id=self.note.pk,
            reason="entered in error",
            today=D(2026, 12, 7),
        )
        self.client.login(username="owner", password=PW)

    def ledger(self, **params):
        params.setdefault("preset", "custom")
        params.setdefault("start", "2026-11-01")
        params.setdefault("end", "2026-12-31")
        return self.client.get(reverse("finance:payments"), params)

    def rows(self, response):
        return [r["txn"] for r in response.context["page"]]

    def txn(self, **kw):
        return FinancialTransaction.objects.get(**kw)


class OverviewTests(Base):
    def test_cards_match_the_existing_collection_report_and_keep_tokens_out_of_revenue(self):
        cards = ws.overview(D(2026, 11, 1), D(2026, 12, 31))
        self.assertEqual(cards["subscription_collections"], 80000 + 150000 + 80000)
        self.assertEqual(cards["token_collections"], 3 * 50000)
        self.assertEqual(cards["token_refunds"], 50000)
        self.assertEqual(cards["token_forfeitures"], 50000)
        self.assertEqual(cards["adjustments_out"], 10000)
        # one source of truth: the same numbers the collections report shows
        report = queries.build("collections", D(2026, 11, 1), D(2026, 12, 31), D(2026, 12, 31))
        totals = {r[0]: r[1] for r in report.sections[0].rows}
        self.assertEqual(cards["subscription_collections"], totals["Subscription fees collected"])
        self.assertEqual(cards["token_collections"], totals["Tokens collected"])
        self.assertEqual(cards["token_refunds"], -totals["Tokens refunded"])
        # tokens are a liability: collected minus refunded minus forfeited is what is still held
        self.assertEqual(cards["token_balance_held"], 50000)
        self.assertEqual(
            cards["token_balance_held"],
            cards["token_collections"] - cards["token_refunds"] - cards["token_forfeitures"],
        )
        # the revenue figure contains no token money
        self.assertEqual(
            cards["subscription_collections"],
            reporting.collections(D(2026, 11, 1), D(2026, 12, 31))["subscription_fees"],
        )

    def test_each_period_only_counts_its_own_dates(self):
        november = ws.overview(D(2026, 11, 1), D(2026, 11, 30))
        december = ws.overview(D(2026, 12, 1), D(2026, 12, 31))
        self.assertEqual(november["subscription_collections"], 310000)
        self.assertEqual(november["token_refunds"], 0)
        self.assertEqual(november["token_forfeitures"], 0)
        self.assertEqual(december["subscription_collections"], 0)
        self.assertEqual(december["token_collections"], 0)
        self.assertEqual(december["token_refunds"], 50000)
        self.assertEqual(december["token_forfeitures"], 50000)  # no cash, by effective date
        self.assertEqual(december["adjustments_out"], 10000)

    def test_boundary_dates_are_inclusive(self):
        self.assertEqual(
            ws.overview(D(2026, 12, 3), D(2026, 12, 3))["token_refunds"], 50000
        )  # first and last day
        self.assertEqual(ws.overview(D(2026, 12, 4), D(2026, 12, 4))["token_refunds"], 0)

    def test_page_shows_the_cards_in_rupees_and_separates_the_three_groups(self):
        html = self.ledger().content.decode()
        for text in (
            "Cash collections",
            "refundable deposits",
            "Adjustments and reversals",
            "₹3,100.00",  # subscriptions
            "₹1,500.00",  # token collections
            "Token balance held now",
        ):
            self.assertIn(text, html)
        self.assertIn("no outstanding subscription balances", html)

    def test_an_empty_period_shows_zero_cards_and_an_empty_ledger(self):
        response = self.ledger(start="2030-01-01", end="2030-01-31")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cards"]["subscription_collections"], 0)
        self.assertContains(response, "No transactions match these filters")


class LedgerTests(Base):
    def test_every_transaction_in_the_period_is_listed_once_with_its_existing_receipt_number(self):
        response = self.ledger()
        txns = self.rows(response)
        ids = [t.pk for t in txns]
        self.assertEqual(len(ids), len(set(ids)))  # no duplicates
        self.assertEqual(
            set(ids), set(FinancialTransaction.objects.values_list("pk", flat=True))
        )  # nothing invented, nothing missing
        html = response.content.decode()
        for t in FinancialTransaction.objects.exclude(receipt_no=""):
            self.assertIn(t.receipt_no, html)
        self.assertIn("SUB-2026-000001", html)
        self.assertIn("TOK-2026-000001", html)
        self.assertIn("REF-2026-000001", html)
        self.assertIn("ADJ-2026-000001", html)

    def test_columns_member_type_mode_reference_and_amount(self):
        html = self.ledger(q="Asha").content.decode()
        self.assertIn("Asha Rao", html)
        self.assertIn(self.a.membership_no, html)
        for text in ("Subscription payment", "Token collection", "Token refund", "Adjustment"):
            self.assertIn(text, html)
        self.assertIn("U1", html)  # the existing UPI reference
        self.assertIn("&minus;₹500.00", html)  # refund: money out
        self.assertIn("UPI", html)
        self.assertIn("Cash", html)

    def test_forfeitures_and_reversals_have_no_receipt_and_no_cash(self):
        html = self.ledger(kind="token_forfeiture").content.decode()
        self.assertIn("Token forfeiture", html)
        self.assertIn("no cash", html)
        forfeiture = self.txn(kind=K.TOKEN_FORFEITURE)
        self.assertEqual(forfeiture.receipt_no, "")
        rows = self.rows(self.ledger(kind="reversal"))
        self.assertEqual([t.kind for t in rows], [K.REVERSAL])
        self.assertEqual(rows[0].receipt_no, "")

    def test_the_original_of_a_reversal_is_flagged_not_changed(self):
        before = self.txn(pk=self.note.pk)
        html = self.ledger(kind="adjustment").content.decode()
        self.assertIn("Reversed", html)
        self.assertEqual(self.txn(pk=self.note.pk).amount_paise, before.amount_paise)

    def test_filter_by_transaction_type(self):
        for kind in K.values:
            rows = self.rows(self.ledger(kind=kind))
            self.assertTrue(rows, kind)
            self.assertEqual({t.kind for t in rows}, {kind})
        self.assertEqual(len(self.rows(self.ledger(kind="token_refund"))), 1)

    def test_filter_by_member_name_member_id_and_member_link(self):
        self.assertEqual({t.member_id for t in self.rows(self.ledger(q="Ravi"))}, {self.b.pk})
        self.assertEqual(
            {t.member_id for t in self.rows(self.ledger(q=self.c.membership_no))}, {self.c.pk}
        )
        linked = self.ledger(member=self.b.pk)
        self.assertEqual({t.member_id for t in self.rows(linked)}, {self.b.pk})
        self.assertContains(linked, "Show all members")
        self.assertEqual(self.rows(self.ledger(q="nobody")), [])

    def test_filter_by_payment_mode_lists_a_split_payment_once(self):
        d = register(
            self.owner,
            "6m",
            "Divya Nair",
            "9000011111",
            today=D(2026, 11, 20),
            subscription_entries=[Entry("cash", 30000), Entry("upi", 50000, reference="SPLIT-1")],
        ).member
        cash = self.rows(self.ledger(mode="cash"))
        upi = self.rows(self.ledger(mode="upi"))
        split = self.txn(member=d, kind=K.SUBSCRIPTION_PAYMENT)
        self.assertEqual([t.pk for t in cash].count(split.pk), 1)
        self.assertEqual([t.pk for t in upi].count(split.pk), 1)
        self.assertIn(
            "Cash + UPI", self.ledger(q="Divya", kind="subscription_payment").content.decode()
        )
        none = self.rows(self.ledger(mode="none"))
        self.assertEqual({t.kind for t in none}, {K.TOKEN_FORFEITURE, K.ADJUSTMENT, K.REVERSAL})
        self.assertTrue(all(not t.entries.exists() for t in none))

    def test_filter_by_receipt_number(self):
        rows = self.rows(self.ledger(receipt="SUB-2026-000002"))
        self.assertEqual([t.receipt_no for t in rows], ["SUB-2026-000002"])
        self.assertEqual(len(self.rows(self.ledger(receipt="SUB-"))), 3)
        self.assertEqual(self.rows(self.ledger(receipt="ZZZ")), [])

    def test_filters_combine_and_survive_pagination_links(self):
        response = self.ledger(q="Asha", kind="token_collection", mode="upi")
        rows = self.rows(response)
        self.assertEqual([(t.member_id, t.kind) for t in rows], [(self.a.pk, K.TOKEN_COLLECTION)])
        with mock.patch.object(ws, "PAGE_SIZE", 2):
            first = self.ledger(q="a")
            self.assertEqual(len(first.context["page"]), 2)
            self.assertContains(first, "Page 1 of")
            self.assertIn("q=a", first.context["qs"])
            second = self.client.get(
                reverse("finance:payments"),
                {
                    "preset": "custom",
                    "start": "2026-11-01",
                    "end": "2026-12-31",
                    "q": "a",
                    "page": 2,
                },
            )
            self.assertEqual(second.context["page"].number, 2)
            first_ids = {r["txn"].pk for r in first.context["page"]}
            second_ids = {r["txn"].pk for r in second.context["page"]}
            self.assertFalse(first_ids & second_ids)

    def test_newest_first_and_only_the_period(self):
        december = self.rows(self.ledger(start="2026-12-01", end="2026-12-31"))
        self.assertTrue(
            all(D(2026, 12, 1) <= t.effective_date <= D(2026, 12, 31) for t in december)
        )
        dates = [t.effective_date for t in self.rows(self.ledger())]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_invalid_inputs_fall_back_safely(self):
        bad = self.client.get(reverse("finance:payments"), {"preset": "nonsense"})
        self.assertEqual(bad.status_code, 200)
        self.assertContains(bad, "Unknown period")
        reversed_dates = self.ledger(start="2026-12-31", end="2026-11-01")
        self.assertContains(reversed_dates, "must not be after")
        junk = self.client.get(
            reverse("finance:payments"),
            {"kind": "'; DROP TABLE x;--", "mode": "zzz", "member": "abc", "q": "\x00x"},
        )
        self.assertEqual(junk.status_code, 200)
        self.assertContains(junk, "not valid")

    def test_no_contact_details_are_shown(self):
        html = self.ledger().content.decode()
        self.assertNotIn(self.a.mobile_e164, html)
        self.assertNotIn("9876543210", html)


class TokenTabTests(Base):
    def get(self, **params):
        return self.client.get(reverse("finance:payments_tokens"), params)

    def test_one_row_per_cycle_with_status_dates_and_amounts(self):
        rows = {r["member"].pk: r for r in self.get().context["page"]}
        self.assertEqual(rows[self.a.pk]["status_label"], "Refunded")
        self.assertEqual(rows[self.b.pk]["status_label"], "Forfeited")
        self.assertEqual(rows[self.c.pk]["status_label"], "Held")
        self.assertEqual(rows[self.c.pk]["amount_paise"], 50000)
        self.assertEqual(rows[self.a.pk]["amount_paise"], 0)
        self.assertEqual(rows[self.a.pk]["collected_on"], D(2026, 11, 11))
        self.assertEqual(rows[self.a.pk]["closed_on"], D(2026, 12, 3))
        self.assertEqual(rows[self.b.pk]["closed_on"], D(2026, 12, 1))
        self.assertEqual(rows[self.b.pk]["forfeiture_reason"], "test")
        self.assertEqual(rows[self.b.pk]["forfeiture_trigger"], "Lost book")

    def test_totals_and_status_filter(self):
        response = self.get()
        counts = {label: t["count"] for label, t in response.context["totals"]}
        self.assertEqual(counts["Held"], 1)
        self.assertEqual(counts["Refunded"], 1)
        self.assertEqual(counts["Forfeited"], 1)
        only = self.get(status="forfeited")
        self.assertEqual([r["member"].pk for r in only.context["page"]], [self.b.pk])
        self.assertEqual(len(self.get(status="bogus").context["page"]), 3)

    def test_date_range_matches_any_token_event(self):
        november = {
            r["member"].pk for r in self.get(start="2026-11-01", end="2026-11-30").context["page"]
        }
        self.assertEqual(november, {self.a.pk, self.b.pk, self.c.pk})  # all collected in November
        december = {
            r["member"].pk for r in self.get(start="2026-12-01", end="2026-12-31").context["page"]
        }
        self.assertEqual(december, {self.a.pk, self.b.pk})  # refund and forfeiture
        self.assertEqual(
            self.get(start="2031-01-01", end="2031-12-31").context["page"].object_list, []
        )
        self.assertContains(self.get(start="x"), "YYYY-MM-DD")
        self.assertContains(self.get(start="2026-12-31", end="2026-01-01"), "must not be after")

    def test_members_needing_a_fresh_token_use_the_existing_renewal_rule(self):
        needs = {m.pk: why for m, why in self.get().context["needs_token"]}
        self.assertEqual(set(needs), {self.a.pk, self.b.pk})  # C holds a token
        self.assertEqual(needs[self.b.pk], "Token forfeited")
        self.assertIn("refunded", needs[self.a.pk])
        # the same members are the ones the renewal rule blocks
        from memberships import services as ms

        for member in (self.a, self.b, self.c):
            blocked = any(
                code in {"token_forfeited", "token_missing"}
                for code, _ in ms.renewal_blockers(member, D(2026, 12, 8))
            )
            self.assertEqual(blocked, member.pk in needs)
        self.assertContains(self.get(), "fresh ₹500.00 token")

    def test_anonymised_and_archived_members_are_not_listed_as_needing_a_token(self):
        members.anonymise_member(self.b, actor=self.owner, reason="request", confirm=True)
        needs = {m.pk for m, _ in self.get().context["needs_token"]}
        self.assertNotIn(self.b.pk, needs)

    def test_forfeiture_reason_is_masked_for_anonymised_members(self):
        members.anonymise_member(self.b, actor=self.owner, reason="request", confirm=True)
        row = next(r for r in self.get().context["page"] if r["member"].pk == self.b.pk)
        self.assertIn("Redacted", row["forfeiture_reason"])

    def test_a_status_with_no_cycles_shows_the_empty_state(self):
        response = self.get(status="reversed")
        self.assertContains(response, "No tokens match these filters")


class EmptyDatabaseTests(TestCase):
    def setUp(self):
        make_owner()
        self.client.login(username="owner", password=PW)

    def test_every_page_renders_with_no_records(self):
        for url in (reverse("finance:payments"), reverse("finance:payments_tokens")):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
        self.assertContains(self.client.get(reverse("finance:payments")), "No transactions match")
        tokens_page = self.client.get(reverse("finance:payments_tokens"))
        self.assertContains(tokens_page, "No tokens match")
        self.assertContains(tokens_page, "No active member needs a new token")


class DetailTests(Base):
    def detail(self, txn):
        return self.client.get(reverse("finance:payments_detail", args=[txn.pk]))

    def test_subscription_payment_details_links_and_receipt(self):
        txn = self.txn(member=self.a, kind=K.SUBSCRIPTION_PAYMENT)
        response = self.detail(txn)
        self.assertEqual(response.status_code, 200)
        for text in (
            "Subscription payment",
            txn.receipt_no,
            self.a.full_name,
            self.a.membership_no,
            "₹800.00",
            "6-month subscription",
            "View / print receipt",
            str(txn.internal_txn_id),
            "Payment entries",
        ):
            self.assertContains(response, text)
        self.assertContains(response, reverse("finance:receipt", args=[txn.receipt_document_id]))
        self.assertEqual(
            self.client.get(reverse("finance:receipt", args=[txn.receipt_document_id])).status_code,
            200,
        )

    def test_token_collection_shows_the_cycle_and_the_same_printout(self):
        txn = self.txn(member=self.a, kind=K.TOKEN_COLLECTION)
        response = self.detail(txn)
        self.assertContains(response, "Cycle 1")
        self.assertContains(response, "Collection")
        self.assertContains(response, "Same receipt printout")  # the combined registration receipt

    def test_forfeiture_details_show_trigger_confirmation_and_no_receipt(self):
        txn = self.txn(kind=K.TOKEN_FORFEITURE)
        response = self.detail(txn)
        self.assertContains(response, "None for this kind of record")
        self.assertContains(response, "Lost book")
        self.assertContains(response, "owner confirmed")
        self.assertContains(response, "no cash")

    def test_reversal_and_original_are_linked_both_ways_and_the_original_is_unchanged(self):
        reversal = self.txn(kind=K.REVERSAL)
        original = self.txn(pk=self.note.pk)
        self.assertContains(self.detail(reversal), "Reverses")
        self.assertContains(
            self.detail(reversal), reverse("finance:payments_detail", args=[original.pk])
        )
        page = self.detail(original)
        self.assertContains(page, "Reversed by")
        self.assertContains(page, "the original record is unchanged")
        self.assertEqual(self.txn(pk=original.pk).amount_paise, 100)

    def test_adjustment_shows_its_reason_and_owner(self):
        page = self.detail(self.txn(pk=self.adjust_out.pk))
        self.assertContains(page, "duplicate token entry")
        self.assertContains(page, "owner")
        self.assertContains(page, self.adjust_out.receipt_no)

    def test_audit_history_is_matched_to_each_transaction(self):
        sub = self.txn(member=self.a, kind=K.SUBSCRIPTION_PAYMENT)
        tok = self.txn(member=self.a, kind=K.TOKEN_COLLECTION)
        refund = self.txn(pk=self.refund.pk)
        forfeiture = self.txn(kind=K.TOKEN_FORFEITURE)
        reversal = self.txn(kind=K.REVERSAL)
        original = self.txn(pk=self.note.pk)

        def actions(txn):
            return {item["event"].action for item in ws.audit_history(txn)}

        self.assertIn("membership.registered", actions(sub))
        self.assertIn("token.collected", actions(tok))
        self.assertNotIn("token.collected", actions(sub))
        self.assertIn("token.refunded", actions(refund))
        self.assertIn("token.forfeited", actions(forfeiture))
        self.assertIn("finance.adjustment", actions(original))
        self.assertIn("finance.reversal", actions(reversal))
        self.assertIn("finance.reversal", actions(original))
        self.assertContains(self.detail(sub), "membership.registered")
        # other members' events never leak in
        other = self.txn(member=self.c, kind=K.SUBSCRIPTION_PAYMENT)
        self.assertEqual(
            {i["event"].pk for i in ws.audit_history(sub)}
            & {i["event"].pk for i in ws.audit_history(other)},
            set(),
        )

    def test_anonymised_members_reasons_and_references_are_masked(self):
        members.anonymise_member(self.a, actor=self.owner, reason="request", confirm=True)
        adjust = self.detail(self.txn(pk=self.adjust_out.pk))
        self.assertNotContains(adjust, "duplicate token entry")
        self.assertContains(adjust, "Redacted")
        self.assertContains(adjust, "Anonymised")
        tok = self.detail(self.txn(member=self.a, kind=K.TOKEN_COLLECTION))
        self.assertNotContains(tok, "U1")
        self.assertNotContains(self.ledger(), "duplicate token entry")

    def test_unknown_transaction_is_404(self):
        self.assertEqual(
            self.client.get(reverse("finance:payments_detail", args=[999999])).status_code, 404
        )

    def test_the_reverse_link_follows_the_existing_rule(self):
        reversible = self.txn(kind=K.TOKEN_FORFEITURE)
        self.assertContains(
            self.detail(reversible), reverse("finance:reverse", args=[reversible.pk])
        )
        subscription = self.txn(member=self.a, kind=K.SUBSCRIPTION_PAYMENT)
        self.assertNotContains(self.detail(subscription), "Reverse this record")  # fees: never
        self.assertNotContains(
            self.detail(self.txn(pk=self.note.pk)), "Reverse this record"
        )  # done


class AccessTests(Base):
    def urls(self):
        txn = self.txn(kind=K.TOKEN_FORFEITURE)
        return [
            reverse("finance:payments"),
            reverse("finance:payments_tokens"),
            reverse("finance:payments_detail", args=[txn.pk]),
        ]

    def test_owner_only(self):
        self.client.logout()
        for url in self.urls():
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("/accounts/login/", response["Location"], url)

    def test_the_pages_never_write_and_accept_only_reads(self):
        def counts():
            return (
                FinancialTransaction.objects.count(),
                PaymentEntry.objects.count(),
                TokenLedgerEntry.objects.count(),
                ReceiptDocument.objects.count(),
                AuditEvent.objects.count(),
            )

        before = counts()
        for url in self.urls():
            self.assertEqual(self.client.get(url).status_code, 200)
            self.assertEqual(self.client.get(url + "?preset=year&kind=adjustment").status_code, 200)
            for method in ("post", "put", "patch", "delete"):
                response = getattr(self.client, method)(url, {"x": "1"})
                self.assertEqual(response.status_code, 405, (method, url))
        self.assertEqual(counts(), before)

    def test_pages_offer_no_form_that_posts_except_sign_out(self):
        for url in self.urls():
            html = self.client.get(url).content.decode()
            self.assertEqual(html.count('method="post"'), 1, url)  # the sign-out button only
            self.assertIn("/accounts/logout/", html.split('method="post"')[1][:80])

    def test_the_sidebar_link_works_and_is_marked_active(self):
        html = self.client.get(reverse("finance:payments")).content.decode()
        self.assertIn(
            f'<a href="{reverse("finance:payments")}" class="active" aria-current="page">',
            html,
        )
        self.assertNotIn('aria-disabled="true"', html)
        other = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertIn(f'href="{reverse("finance:payments")}"', other)


class ExistingWorkflowsUnaffectedTests(TestCase):
    def test_registration_and_renewal_still_post_exactly_the_same_records(self):
        owner = make_owner()
        result = register(owner, "6m", today=D(2026, 11, 11))
        self.assertEqual(FinancialTransaction.objects.count(), 2)
        renew(owner, result.member, "6m", today=D(2026, 11, 12))
        self.assertEqual(FinancialTransaction.objects.count(), 3)
        self.client.login(username="owner", password=PW)
        for url in (reverse("finance:payments"), reverse("finance:payments_tokens")):
            self.client.get(url)
        self.assertEqual(FinancialTransaction.objects.count(), 3)  # viewing created nothing
        receipts = list(
            FinancialTransaction.objects.exclude(receipt_no="").values_list("receipt_no", flat=True)
        )
        self.assertEqual(
            sorted(receipts), ["SUB-2026-000001", "SUB-2026-000002", "TOK-2026-000001"]
        )  # numbering untouched
