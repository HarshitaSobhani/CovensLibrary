"""Owner-decision remediation: H1 amount limits, H3 reason length, D-7 corrections rules."""

import uuid
from datetime import date
from decimal import Decimal

from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TestCase
from django.urls import reverse

from audit.models import AuditEvent
from catalogue import services as catalogue_services
from core.errors import RuleViolation
from core.models import LibrarySetting
from core.money import MAX_PAISE, MAX_REASON_LENGTH, MAX_RUPEES
from finance import allocation, reporting, tokens
from finance import corrections as fc
from finance.models import FinancialTransaction, TokenLedgerEntry
from finance.services import Entry
from memberships import services as ms
from memberships.tests.helpers import PW, TODAY, forfeit, make_owner, register

INT32 = 2**31 - 1


class LimitConstantTests(TestCase):
    def test_the_accepted_limit_is_unchanged_and_needs_64_bit_storage(self):
        self.assertEqual(MAX_PAISE, 4_999_900)  # Rs 49,999.00 (D-16)
        self.assertEqual(MAX_RUPEES, Decimal("49999.00"))
        self.assertEqual(MAX_REASON_LENGTH, 2000)
        self.assertLess(MAX_PAISE, INT32)  # the 64-bit columns are kept deliberately (headroom)

    def test_every_money_column_is_bigint(self):
        columns = [
            ("finance_financialtransaction", "amount_paise"),
            ("finance_paymententry", "amount_paise"),
            ("finance_tokencycle", "amount_snapshot_paise"),
            ("finance_tokenledgerentry", "delta_paise"),
            ("memberships_subscriptionplan", "price_paise"),
            ("memberships_membersubscription", "price_paise_snapshot"),
            ("core_librarysetting", "token_amount_paise"),
            ("catalogue_acquisition", "unit_cost_paise"),
        ]
        with connection.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema='public' AND column_name ~ '_paise'"
            )
            found = {(t, c): d for t, c, d in cur.fetchall()}
        self.assertEqual(set(found), set(columns), "a paise column is missing from this test")
        self.assertEqual({d for d in found.values()}, {"bigint"})


class AmountBoundaryTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member = register(self.owner).member

    def adjust(self, amount, direction="in", **kw):
        return fc.post_adjustment(
            actor=self.owner,
            member=self.member,
            direction=direction,
            amount_paise=amount,
            reason="boundary",
            owner_approved=True,
            confirm_not_fee_refund=True,
            entries=[Entry("cash", amount)],
            today=TODAY,
            **kw,
        )

    def code(self, fn, *a, **k):
        with self.assertRaises(RuleViolation) as ctx:
            fn(*a, **k)
        return ctx.exception.code

    def test_the_maximum_amount_is_accepted_and_stored_exactly(self):
        txn = self.adjust(MAX_PAISE).transactions[0]
        txn.refresh_from_db()
        self.assertEqual(txn.amount_paise, MAX_PAISE)
        self.assertEqual(txn.entries.get().amount_paise, MAX_PAISE)

    def test_amounts_up_to_the_maximum_work(self):
        for amount in (1, 80_000, 1_234_567, MAX_PAISE - 1):
            self.assertEqual(self.adjust(amount).transactions[0].amount_paise, amount)

    def test_one_paisa_above_the_maximum_is_a_clear_validation_error(self):
        self.assertEqual(self.code(self.adjust, MAX_PAISE + 1), "amount_too_large")
        self.assertEqual(self.code(self.adjust, 10**15), "amount_too_large")
        self.assertEqual(FinancialTransaction.objects.filter(kind="adjustment").count(), 0)

    def test_a_single_payment_entry_above_the_maximum_is_rejected_too(self):
        error = self.code(
            fc.post_adjustment,
            actor=self.owner,
            member=self.member,
            direction="in",
            amount_paise=MAX_PAISE,
            reason="r",
            owner_approved=True,
            entries=[Entry("cash", MAX_PAISE), Entry("upi", MAX_PAISE + 1)],
            today=TODAY,
        )
        self.assertEqual(error, "amount_too_large")

    def test_non_integers_remain_invalid(self):
        for bad in (10.5, "100", None, True, -1, 0):
            self.assertEqual(self.code(self.adjust, bad), "amount_invalid")

    def test_totals_and_reports_handle_amounts_above_32_bits(self):
        self.adjust(MAX_PAISE)
        self.adjust(MAX_PAISE)
        out = reporting.collections(TODAY, TODAY)
        self.assertEqual(out["adjustments_in"], 2 * MAX_PAISE)
        self.assertEqual(out["by_method"]["cash"], 80000 + 2 * MAX_PAISE)

    def test_allocation_stays_exact_at_the_maximum_price(self):
        parts = allocation.allocate_by_month(MAX_PAISE, date(2026, 11, 11), date(2027, 5, 11))
        self.assertEqual(sum(parts.values()), MAX_PAISE)

    def test_receipt_pages_render_the_maximum(self):
        self.client.login(username="owner", password=PW)
        doc = self.adjust(MAX_PAISE).document
        page = self.client.get(reverse("finance:receipt", args=[doc.pk]))
        self.assertContains(page, "₹49,999.00")
        pdf = self.client.get(reverse("finance:receipt_pdf", args=[doc.pk]))
        self.assertTrue(pdf.content.startswith(b"%PDF"))

    def test_a_token_at_the_maximum_collects_forfeits_and_refunds_through_the_triggers(self):
        s = LibrarySetting.load()
        s.token_amount_paise = MAX_PAISE
        s.save()
        other = ms.register_member(
            actor=self.owner,
            member_fields={"full_name": "Big", "national_number": "9811100123", "region": "IN"},
            plan_code="6m",
            subscription_entries=[Entry("cash", 80000)],
            token_entries=[Entry("cash", MAX_PAISE)],
            today=TODAY,
        ).member
        cycle = tokens.latest_cycle(other)
        self.assertEqual(
            (cycle.amount_snapshot_paise, tokens.balance_paise(cycle)), (MAX_PAISE,) * 2
        )
        fc.refund_token(
            actor=self.owner, member=other, entries=[Entry("cash", MAX_PAISE)], today=TODAY
        )
        self.assertEqual(tokens.balance_paise(cycle), 0)

    def test_database_checks_enforce_the_maximum_even_for_direct_sql(self):
        txn = FinancialTransaction.objects.first()
        statements = [
            (
                "update-free insert of an over-limit entry",
                "INSERT INTO finance_paymententry (transaction_id, method, amount_paise, paid_on, "
                "reference, recorded_at) VALUES (%s, 'cash', %s, '2026-11-11', '', now())",
                [txn.pk, MAX_PAISE + 1],
            ),
            (
                "over-limit plan price",
                "UPDATE memberships_subscriptionplan SET price_paise = %s WHERE code = '6m'",
                [MAX_PAISE + 1],
            ),
            (
                "over-limit token setting",
                "UPDATE core_librarysetting SET token_amount_paise = %s",
                [MAX_PAISE + 1],
            ),
            (
                "over-limit unit cost",
                "INSERT INTO catalogue_acquisition (edition_id, source, donor_name, donor_contact, "
                "unit_cost_paise, notes) VALUES (1, 'purchased', '', '', %s, '')",
                [MAX_PAISE + 1],
            ),
        ]
        for name, sql, params in statements:
            with self.subTest(name), self.assertRaises(DatabaseError):
                with transaction.atomic(), connection.cursor() as cur:
                    cur.execute(sql, params)

    def test_database_accepts_exactly_the_maximum_in_every_check(self):
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(
                "UPDATE memberships_subscriptionplan SET price_paise = %s WHERE code='12m'",
                [MAX_PAISE],
            )
            cur.execute("SELECT price_paise FROM memberships_subscriptionplan WHERE code='12m'")
            self.assertEqual(cur.fetchone()[0], MAX_PAISE)
            transaction.set_rollback(True)


class AmountInterfaceTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.client.login(username="owner", password=PW)
        self.member = register(self.owner, today=date.today()).member

    def post(self, amount, **extra):
        data = {
            "direction": "in",
            "amount": amount,
            "pay_cash": amount,
            "reason": "limit test",
            "approved": "on",
            "request_key": str(uuid.uuid4()),
            **extra,
        }
        return self.client.post(reverse("finance:adjust", args=[self.member.pk]), data)

    def test_the_form_accepts_the_maximum(self):
        self.assertEqual(self.post("49999.00").status_code, 302)
        self.assertEqual(
            FinancialTransaction.objects.get(kind="adjustment").amount_paise, MAX_PAISE
        )

    def test_above_the_maximum_is_a_form_error_not_a_server_error(self):
        for amount in ("49999.01", "50000", "100000000.00", "49999.005", "30000000000", "-1"):
            with self.subTest(amount=amount):
                r = self.post(amount)
                self.assertEqual(r.status_code, 200)
                self.assertContains(r, "text-danger")
        self.assertEqual(FinancialTransaction.objects.filter(kind="adjustment").count(), 0)

    def test_catalogue_unit_cost_above_the_maximum_is_a_form_error(self):
        from catalogue.models import Category

        edition = catalogue_services.create_edition(
            actor=self.owner, title="T", category=Category.objects.create(name="C")
        )
        url = reverse("catalogue:copies_add", args=[edition.pk])
        base = {
            "count": 1,
            "source": "purchased",
            "donor_name": "",
            "acquired_on": "",
            "shelf_location": "",
            "notes": "",
        }
        bad = self.client.post(url, {**base, "unit_cost_rupees": "49999.01"})
        self.assertEqual(bad.status_code, 200)
        ok = self.client.post(url, {**base, "unit_cost_rupees": "49999.00"})
        self.assertEqual(ok.status_code, 302)
        with self.assertRaises(RuleViolation) as ctx:
            catalogue_services.add_copies(
                edition, 1, actor=self.owner, source="purchased", unit_cost_paise=MAX_PAISE + 1
            )
        self.assertEqual(ctx.exception.code, "amount_too_large")


class ReasonLengthTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member = register(self.owner).member
        self.long = "x" * (MAX_REASON_LENGTH + 1)
        self.exact = "y" * MAX_REASON_LENGTH

    def code(self, fn, **kw):
        with self.assertRaises(RuleViolation) as ctx:
            fn(**kw)
        return ctx.exception.code

    def adjust(self, reason, **kw):
        return fc.post_adjustment(
            actor=self.owner,
            member=self.member,
            direction="none",
            amount_paise=100,
            reason=reason,
            owner_approved=True,
            today=TODAY,
            **kw,
        )

    def test_a_reason_over_the_limit_is_rejected_never_truncated(self):
        self.assertEqual(self.code(self.adjust, reason=self.long), "reason_too_long")
        self.assertEqual(FinancialTransaction.objects.filter(kind="adjustment").count(), 0)
        self.assertFalse(AuditEvent.objects.filter(action="finance.adjustment").exists())

    def test_a_reason_exactly_at_the_limit_is_stored_in_full_everywhere(self):
        txn = self.adjust(self.exact).transactions[0]
        self.assertEqual(txn.reason, self.exact)
        self.assertEqual(
            AuditEvent.objects.get(action="finance.adjustment").summary["reason"], self.exact
        )

    def test_every_reason_field_has_the_same_rule(self):
        original = forfeit(self.owner, self.member, today=TODAY).transactions[0]
        cases = {
            "forfeiture": lambda: fc.forfeit_token(
                actor=self.owner,
                member=self.member,
                trigger="poor",
                owner_confirmed=True,
                reason=self.long,
                today=TODAY,
            ),
            "reversal": lambda: fc.reverse_transaction(
                actor=self.owner, transaction_id=original.pk, reason=self.long, today=TODAY
            ),
            "backdate": lambda: self.adjust(
                "ok",
                payment_date=date(2026, 11, 1),
                backdate_reason=self.long,
                backdate_confirmed=True,
            ),
            "cancel": lambda: ms.cancel_subscription(
                actor=self.owner, subscription=self.member.subscriptions.get(), reason=self.long
            ),
        }
        for name, fn in cases.items():
            with self.subTest(name), self.assertRaises(RuleViolation) as ctx:
                fn()
            self.assertEqual(ctx.exception.code, "reason_too_long", name)
        self.assertEqual(FinancialTransaction.objects.filter(kind="reversal").count(), 0)
        self.assertIsNone(self.member.subscriptions.get().cancelled_at)

    def test_confirmed_forfeiture_reason_is_kept_whole_in_the_ledger(self):
        fc.forfeit_token(
            actor=self.owner,
            member=self.member,
            trigger="lost",
            owner_confirmed=True,
            reason=self.exact,
            today=TODAY,
        )
        self.assertEqual(TokenLedgerEntry.objects.get(entry_type="forfeiture").reason, self.exact)

    def test_the_interface_shows_a_validation_error(self):
        self.client.login(username="owner", password=PW)
        r = self.client.post(
            reverse("finance:adjust", args=[self.member.pk]),
            {
                "direction": "none",
                "amount": "1",
                "reason": self.long,
                "approved": "on",
                "request_key": str(uuid.uuid4()),
            },
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "at most 2000 characters")
        self.assertEqual(FinancialTransaction.objects.filter(kind="adjustment").count(), 0)


class AdjustmentNotAFeeRefundTests(TestCase):
    """D-7: an adjustment is never a cash refund of a subscription fee."""

    def setUp(self):
        self.owner = make_owner()
        self.member = register(self.owner).member
        self.fee = FinancialTransaction.objects.get(kind="subscription_payment")

    def adjust(self, direction="out", **kw):
        kw.setdefault("confirm_not_fee_refund", True)
        entries = [] if direction == "none" else [Entry("cash", 700)]
        return fc.post_adjustment(
            actor=self.owner,
            member=self.member,
            direction=direction,
            amount_paise=700,
            reason="accounting",
            owner_approved=True,
            entries=entries,
            today=TODAY,
            **kw,
        )

    def code(self, **kw):
        with self.assertRaises(RuleViolation) as ctx:
            self.adjust(**kw)
        return ctx.exception.code

    def test_money_out_needs_the_explicit_statement_that_it_is_not_a_fee_refund(self):
        self.assertEqual(self.code(confirm_not_fee_refund=False), "fee_refund_ack_required")
        self.assertEqual(FinancialTransaction.objects.filter(kind="adjustment").count(), 0)

    def test_money_out_cannot_reference_a_subscription_payment(self):
        self.assertEqual(self.code(related_id=self.fee.pk), "fee_refund_blocked")

    def test_other_accounting_adjustments_stay_possible(self):
        for direction, related in (("in", self.fee.pk), ("none", self.fee.pk), ("out", None)):
            with self.subTest(direction=direction):
                txn = self.adjust(direction, related_id=related).transactions[0]
                self.assertEqual(txn.kind, "adjustment")
        forfeiture = forfeit(self.owner, self.member).transactions[0]
        recovered = self.adjust("out", related_id=forfeiture.pk).transactions[0]  # B12
        self.assertEqual(recovered.adjusts_id, forfeiture.pk)

    def test_the_audit_record_keeps_the_acknowledgement_and_reason(self):
        self.adjust("out")
        event = AuditEvent.objects.filter(action="finance.adjustment").latest("id")
        self.assertTrue(event.summary["not_a_fee_refund_confirmed"])
        self.assertEqual(event.summary["reason"], "accounting")

    def test_subscription_payments_still_cannot_be_reversed(self):
        with self.assertRaises(RuleViolation) as ctx:
            fc.reverse_transaction(
                actor=self.owner,
                transaction_id=self.fee.pk,
                reason="x",
                entries=[Entry("cash", 80000)],
                today=TODAY,
            )
        self.assertEqual(ctx.exception.code, "fee_not_reversible")

    def test_the_interface_requires_the_acknowledgement_for_money_out(self):
        self.client.login(username="owner", password=PW)
        url = reverse("finance:adjust", args=[self.member.pk])
        data = {
            "direction": "out",
            "amount": "7",
            "pay_cash": "7",
            "reason": "r",
            "approved": "on",
            "request_key": str(uuid.uuid4()),
        }
        self.assertContains(self.client.post(url, data), "not a refund of a subscription fee")
        self.assertEqual(
            self.client.post(url, {**data, "confirm_not_fee_refund": "on"}).status_code, 302
        )


class CorrectionRuleTriggerTests(TestCase):
    """The same rules hold for raw SQL (database trigger finance_txn_correction_rules)."""

    def setUp(self):
        self.owner = make_owner()
        self.member = register(self.owner).member
        self.fee = FinancialTransaction.objects.get(kind="subscription_payment")
        self.tok = FinancialTransaction.objects.get(kind="token_collection")

    def insert(self, kind, flow, reverses=None, adjusts=None):
        n = FinancialTransaction.objects.count() + 1000
        receipt = {"adjustment": ("adjustment", f"ADJ-2026-{n:06d}")}.get(kind, ("", ""))
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(
                "INSERT INTO finance_financialtransaction (internal_txn_id, member_id, kind, "
                "cash_flow, amount_paise, effective_date, processed_at, receipt_sequence, "
                "receipt_issued_on, receipt_year, receipt_no, receipt_document_id, reverses_id, "
                "adjusts_id, reason, backdated, backdate_reason, backdate_financial_confirmed, "
                "recorded_by_id, recorded_by_system) VALUES (gen_random_uuid(), %s, %s, %s, 100, "
                "'2026-11-11', now(), %s, %s, %s, %s, %s, %s, %s, 'r', false, '', false, %s, '')",
                [
                    self.member.pk,
                    kind,
                    flow,
                    receipt[0],
                    "2026-11-11" if receipt[0] else None,
                    2026 if receipt[0] else None,
                    receipt[1],
                    self.fee.receipt_document_id if receipt[0] else None,
                    reverses,
                    adjusts,
                    self.owner.pk,
                ],
            )
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")

    def test_a_subscription_payment_cannot_be_reversed_by_direct_sql(self):
        with self.assertRaisesMessage(DatabaseError, "subscription_payment cannot be reversed"):
            self.insert("reversal", "out", reverses=self.fee.pk)

    def test_a_refund_and_a_reversal_cannot_be_reversed_by_direct_sql(self):
        refund = fc.refund_token(
            actor=self.owner, member=self.member, entries=[Entry("cash", 50000)], today=TODAY
        ).transactions[0]
        with self.assertRaisesMessage(DatabaseError, "token_refund cannot be reversed"):
            self.insert("reversal", "in", reverses=refund.pk)

    def test_money_out_adjustment_against_a_fee_is_blocked_by_direct_sql(self):
        with self.assertRaisesMessage(DatabaseError, "cannot pay out against a subscription"):
            self.insert("adjustment", "out", adjusts=self.fee.pk)

    def test_the_trigger_exists_is_enabled_and_pins_its_search_path(self):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT t.tgenabled, p.proconfig FROM pg_trigger t "
                "JOIN pg_proc p ON p.oid = t.tgfoid "
                "WHERE t.tgname = 'finance_txn_correction_rules'"
            )
            enabled, config = cur.fetchone()
        self.assertEqual((enabled, config), ("O", ["search_path=pg_catalog"]))


class ApprovedLimitLiteralTests(TestCase):
    """D-16 final limits, written as literals so the constants cannot drift unnoticed."""

    def setUp(self):
        self.owner = make_owner()
        self.member = register(self.owner).member

    def adjust(self, paise, reason="r"):
        return fc.post_adjustment(
            actor=self.owner,
            member=self.member,
            direction="in",
            amount_paise=paise,
            reason=reason,
            owner_approved=True,
            entries=[Entry("cash", paise)],
            today=TODAY,
        )

    def test_the_maximum_amount_is_exactly_rs_49999(self):
        self.assertEqual(self.adjust(4_999_900).transactions[0].amount_paise, 4_999_900)
        with self.assertRaises(RuleViolation) as ctx:
            self.adjust(4_999_901)
        self.assertEqual(ctx.exception.code, "amount_too_large")
        self.assertIn("49,999.00", ctx.exception.message)

    def test_the_maximum_reason_is_exactly_2000_characters(self):
        self.assertEqual(len(self.adjust(100, "r" * 2000).transactions[0].reason), 2000)
        with self.assertRaises(RuleViolation) as ctx:
            self.adjust(100, "r" * 2001)
        self.assertEqual(ctx.exception.code, "reason_too_long")

    def test_database_checks_use_the_same_literal_limit_on_every_money_column(self):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname ~ '_within_limit$' ORDER BY 1"
            )
            rows = cur.fetchall()
        self.assertEqual(len(rows), 8)
        for name, definition in rows:
            with self.subTest(name):
                self.assertIn("4999900", definition)
                self.assertNotIn("9999999999", definition)

    def test_the_database_refuses_one_paisa_above_the_limit(self):
        txn = FinancialTransaction.objects.first()
        for amount, ok in ((4_999_901, False), (4_999_900, True)):
            with self.subTest(amount=amount):
                try:
                    with transaction.atomic(), connection.cursor() as cur:
                        cur.execute(
                            "INSERT INTO finance_paymententry (transaction_id, method, "
                            "amount_paise, paid_on, reference, recorded_at) "
                            "VALUES (%s, 'cash', %s, '2026-11-11', '', now())",
                            [txn.pk, amount],
                        )
                        transaction.set_rollback(True)
                    self.assertTrue(ok)
                except IntegrityError:
                    self.assertFalse(ok)

    def test_money_columns_remain_64_bit(self):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema='public' AND column_name ~ '_paise'"
            )
            self.assertEqual({r[0] for r in cur.fetchall()}, {"bigint"})
